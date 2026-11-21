"""Proposal -> pull request -> human review -> merge eligibility -> deploy.

Two independent controls must BOTH pass before a context change reaches production:
1. Groundwork approval: an authorized human reviewer approves the CURRENT revision in Slack
   (``ProposalStore.decide``).
2. GitHub: the PR is merged through the protected base branch (required reviews + required checks,
   configured by a repository admin). By default humans merge; ``auto_merge`` only merges once the
   Slack approval, passing checks, an unchanged validated head SHA and (if required) a human GitHub
   approval are all present.
Deployment then takes ``context/`` from the merge commit on the base branch, never from a PR head.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from groundwork.config.settings import Settings
from groundwork.product.audit import AuditLog
from groundwork.product.config import ProductConfig
from groundwork.product.deployment import ContextDeployer
from groundwork.product.github import GitHubClient, GitHubError
from groundwork.product.proposals.models import TRANSITIONS, State
from groundwork.product.proposals.patches import stage
from groundwork.product.proposals.store import ProposalRow, ProposalStore


def pr_body(row: ProposalRow) -> str:
    p = row.proposal
    d = p.draft
    v = row.validation or {}
    reg = v.get("regression") or {}
    rep = (v.get("reproduction") or {}).get("questions") or {}
    lines = [
        f"<!-- groundwork-proposal: {p.proposal_id} revision {p.revision} -->",
        f"## Context problem\n**{p.category}** — {d.deficiency}",
        "## Evidence (observed, not generated)",
        *[f"- `{e.kind}` `{e.ref}` — {e.detail}" for e in d.evidence],
        "## Proposed fix",
        d.summary,
        *[
            f"- `{op.op}` " + ", ".join(f"{k}={v!r}"[:160] for k, v in op.model_dump().items() if k != "op")
            for op in d.operations
        ],
        "## Before / after context diff",
        *[
            f"<details><summary>{df.path}</summary>\n\n```diff\n{df.unified_diff[:12000]}\n```\n</details>"
            for df in p.diffs
        ],
        "## Evaluation",
        f"- Validation verdict: **{v.get('verdict', 'not run')}** (repetitions: {v.get('repetitions', '-')})",
        f"- Regression cases: {reg.get('cases', '-')} · baseline correct {reg.get('baseline_correct', '-')} · candidate correct {reg.get('candidate_correct', '-')}",  # noqa: E501 - long literal
        f"- Regressed: {reg.get('regressed_cases', '-')} · improved: {reg.get('improved_cases', '-')}",
        f"- Privacy violations: {reg.get('privacy_violations', '-')} · security violations: {reg.get('security_violations', '-')}",  # noqa: E501 - long literal
        f"- Eval runs: baseline `{reg.get('baseline_run_id', '-')}`, candidate `{reg.get('candidate_run_id', '-')}`",
        *[
            f"- Original question reproduced: before {q['before_outcomes']} → after {q['after_outcomes']}"
            for q in rep.values()
        ],
        "## Targeted regression tests",
        f"Eval cases touching the affected objects: {', '.join(reg.get('case_ids', [])) or 'none'}",
        "## Assumptions",
        *[f"- {a}" for a in d.assumptions or ["none stated"]],
        "## Risks",
        *[f"- {r}" for r in d.risks or ["none stated"]],
        "## Security impact",
        "- Touches only allow-listed context files (checked by Groundwork and by the `proposal-scope` CI job).",
        "- No certification fields are added; every changed object is `draft` until a human certifies it.",
        f"- Review tier: **{p.review_tier}**"
        + (" (metric SQL or deprecation: elevated reviewer required)" if p.review_tier == "elevated" else ""),
        "## Approval requirements",
        "1. An authorized Groundwork reviewer approves this exact revision in Slack.",
        "2. Required GitHub reviews and status checks pass on the protected base branch.",
        "3. Merge happens through GitHub; deployment uses the merge commit only.",
        "## Traces",
        *[f"- `{t}`" for t in p.trace_ids],
        f"\n_Drafted by `{p.author}` ({p.author_type}). AI-generated; not certified._",
    ]
    return "\n".join(lines)


class ProposalWorkflow:
    def __init__(
        self,
        settings: Settings,
        config: ProductConfig,
        proposals: ProposalStore,
        audit: AuditLog,
        deployer: ContextDeployer,
        *,
        github_factory: Callable[[], GitHubClient],
        context_dir: Callable[[], Path],
        notify_review: Callable[[ProposalRow], str | None] | None = None,
        alert: Callable[[str], None] | None = None,
    ) -> None:
        self.settings, self.config, self.proposals, self.audit = settings, config, proposals, audit
        self.deployer = deployer
        self.github_factory = github_factory
        self.context_dir = context_dir
        self.notify_review = notify_review
        self.alert = alert or (lambda _m: None)

    def _gh_fail(self, pid: str, op: str, exc: GitHubError) -> None:
        self.audit.append(
            action="github.operation_failed",
            actor_id="system.github",
            actor_type="system",
            resource_type="proposal",
            resource_id=pid,
            details={
                "operation": op,
                "status": exc.status,
                "retryable": exc.retryable,
                "error": str(exc)[:300],
            },
        )

    def open_pull_request(self, proposal_id: str) -> ProposalRow:
        """Idempotent and resumable: branch, commit and PR are each reused if they already exist."""
        row = self.proposals.get(proposal_id)
        if row is None:
            raise KeyError(proposal_id)
        if row.state != State.AWAITING_REVIEW:
            raise ValueError(f"PRs are opened only for validated proposals (state is {row.state})")
        gh = self.github_factory()
        branch = row.raw["branch"] or f"{self.config.github.branch_prefix}{proposal_id}"
        with tempfile.TemporaryDirectory(prefix="cf-pr-") as t:
            staged = stage(
                repo_root=self.settings.root,
                context_dir=self.context_dir(),
                serving_path=self.settings.serving_path,
                ops=list(row.proposal.draft.operations),
                author=row.proposal.author,
                evidence_refs=[f"{e.kind}:{e.ref}" for e in row.proposal.draft.evidence][:10],
                work_dir=Path(t) / "repo",
            )
            if staged.validation_errors:
                raise ValueError("staged context no longer validates; re-validate the proposal")
            files = {
                rel: (Path(t) / "repo" / rel).read_text(encoding="utf-8") for rel in staged.changed_files
            }
        try:
            gh.ensure_branch(branch, self.config.github.base_branch)
            self.proposals.transition(
                proposal_id, row.state, actor="system.github", updates={"branch": branch}
            )
            sha = gh.commit_files(
                branch, files, f"context: {row.proposal.title} ({proposal_id} r{row.revision})"
            )
            self.proposals.transition(
                proposal_id, row.state, actor="system.github", updates={"head_sha": sha}
            )
            reviewers = [
                r.github_login
                for r in self.config.reviewers_for(
                    row.proposal.domains, elevated=row.review_tier == "elevated"
                )
                if r.github_login
            ]
            pr = gh.open_pr(
                branch=branch,
                base=self.config.github.base_branch,
                title=f"[Groundwork] {row.proposal.title}",
                body=pr_body(row),
                labels=self.config.github.labels,
                reviewers=reviewers,
            )
        except GitHubError as exc:
            self._gh_fail(proposal_id, "open_pull_request", exc)
            raise
        created = row.raw["pr_number"] is None
        self.proposals.transition(
            proposal_id,
            row.state,
            actor="system.github",
            updates={"pr_number": pr.number, "pr_url": pr.url, "head_sha": pr.head_sha},
        )
        self.audit.append(
            action="pr.created" if created else "pr.updated",
            actor_id="system.github",
            actor_type="system",
            resource_type="proposal",
            resource_id=proposal_id,
            github_ref=pr.url,
            details={
                "number": pr.number,
                "head_sha": pr.head_sha,
                "branch": branch,
                "revision": row.revision,
            },
        )
        row2 = self.proposals.get(proposal_id)
        assert row2 is not None
        if self.notify_review and not row2.raw["review_message_ts"]:
            ts = self.notify_review(row2)
            if ts:
                self.proposals.transition(
                    proposal_id, row2.state, actor="system.slack", updates={"review_message_ts": ts}
                )
                self.audit.append(
                    action="review.requested",
                    actor_id="system.slack",
                    actor_type="system",
                    resource_type="proposal",
                    resource_id=proposal_id,
                    github_ref=pr.url,
                    details={"revision": row2.revision, "tier": row2.review_tier},
                )
        out = self.proposals.get(proposal_id)
        assert out is not None
        return out

    def sync(self, proposal_id: str) -> State:
        """Reconcile with GitHub: merged / closed / head changed. Safe to call repeatedly."""
        row = self.proposals.get(proposal_id)
        if row is None or row.raw["pr_number"] is None:
            raise ValueError("proposal has no pull request")
        gh = self.github_factory()
        try:
            pr = gh.get_pr(int(row.raw["pr_number"]))
        except GitHubError as exc:
            self._gh_fail(proposal_id, "sync", exc)
            raise
        if pr.merged:
            if row.state == State.MERGE_ELIGIBLE:
                self.proposals.transition(
                    proposal_id,
                    State.MERGED,
                    actor="system.github",
                    github_ref=pr.url,
                    updates={"merge_commit": pr.merge_commit_sha},
                )
                self.audit.append(
                    action="pr.merged",
                    actor_id="system.github",
                    actor_type="system",
                    resource_type="proposal",
                    resource_id=proposal_id,
                    github_ref=pr.url,
                    details={"merge_commit": pr.merge_commit_sha},
                )
                return State.MERGED
            if row.state not in (State.MERGED, State.DEPLOYED):
                # Merged on GitHub without a Groundwork approval of this revision: do NOT deploy.
                self.proposals.transition(
                    proposal_id,
                    State.BLOCKED,
                    actor="system.github",
                    github_ref=pr.url,
                    reason="PR merged without Groundwork approval/eligibility; deployment refused",
                )
                self.alert(f"Proposal {proposal_id}: PR merged outside the approval workflow; not deployed")
                return State.BLOCKED
            return row.state
        if pr.state == "closed":
            if row.state not in (State.REJECTED, State.SUPERSEDED, State.DEPLOYED):
                to = State.REJECTED if State.REJECTED in TRANSITIONS[row.state] else State.SUPERSEDED
                self.proposals.transition(
                    proposal_id,
                    to,
                    actor="system.github",
                    reason="PR closed without merge",
                    github_ref=pr.url,
                )
                self.audit.append(
                    action="pr.closed",
                    actor_id="system.github",
                    actor_type="system",
                    resource_type="proposal",
                    resource_id=proposal_id,
                    github_ref=pr.url,
                    details={},
                )
                return to
            return row.state
        if (
            row.raw["head_sha"]
            and pr.head_sha != row.raw["head_sha"]
            and row.state in (State.AWAITING_REVIEW, State.APPROVED, State.MERGE_ELIGIBLE)
        ):
            # Someone pushed to the PR branch: the validated/approved content is no longer what is in the PR.
            self.proposals.transition(
                proposal_id,
                State.PROPOSED,
                actor="system.github",
                github_ref=pr.url,
                reason=f"PR head changed {row.raw['head_sha'][:8]} -> {pr.head_sha[:8]}; re-validation and re-approval required",
            )
            return State.PROPOSED
        return row.state

    def check_merge_eligibility(self, proposal_id: str) -> tuple[bool, list[str]]:
        row = self.proposals.get(proposal_id)
        if row is None:
            raise KeyError(proposal_id)
        reasons: list[str] = []
        if row.state != State.APPROVED:
            reasons.append(f"state is {row.state.value}, not APPROVED")
        if row.raw["decided_revision"] != row.revision:
            reasons.append("approval does not cover the current revision")
        if (row.validation or {}).get("verdict") != State.AWAITING_REVIEW.value:
            reasons.append("validation did not pass for this revision")
        if row.raw["pr_number"] is None:
            reasons.append("no pull request")
        if reasons:
            return False, reasons
        gh = self.github_factory()
        pr = gh.get_pr(int(row.raw["pr_number"]))
        if pr.state != "open":
            reasons.append(f"PR is {pr.state}")
        if pr.head_sha != row.raw["head_sha"]:
            reasons.append("PR head differs from the validated commit")
        checks = gh.checks_state(pr.head_sha)
        if checks != "success":
            reasons.append(f"required checks are '{checks}'")
        if self.config.github.require_github_approval and not gh.human_approved(pr.number, pr.head_sha):
            reasons.append("no human GitHub approval on the current head commit")
        if reasons:
            return False, reasons
        self.proposals.transition(
            proposal_id,
            State.MERGE_ELIGIBLE,
            actor="system.governance",
            github_ref=pr.url,
            reason="Slack approval + checks + GitHub review present",
        )
        if self.config.github.auto_merge:
            gh.allow_merge = True
            sha = gh.merge(pr.number, pr.head_sha, f"[Groundwork] {row.proposal.title} ({proposal_id})")
            self.audit.append(
                action="pr.merged",
                actor_id="system.governance",
                actor_type="system",
                resource_type="proposal",
                resource_id=proposal_id,
                github_ref=pr.url,
                details={"merge_commit": sha, "auto_merge": True},
            )
            self.sync(proposal_id)
        return True, []

    def deploy_merged(self, proposal_id: str, *, actor: str) -> Any:
        row = self.proposals.get(proposal_id)
        if row is None or row.state != State.MERGED or not row.raw["merge_commit"]:
            raise ValueError("only MERGED proposals with a merge commit can be deployed")
        rel = self.deployer.deploy_git_commit(
            self.settings.root,
            row.raw["merge_commit"],
            base_branch=self.config.github.base_branch,
            actor=actor,
            proposal_id=proposal_id,
        )
        self.proposals.transition(
            proposal_id, State.DEPLOYED, actor=actor, actor_type="human", reason=f"deployed {rel.version}"
        )
        return rel
