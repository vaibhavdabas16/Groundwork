"""Evaluation-driven validation of a proposed context change (closed loop, before any release).

Steps, all recorded in ``validation_json`` and the audit log:

1. **Static**: re-stage the proposal's operations on the CURRENT deployed context (detects base
   drift), full schema/reference validation, allow-listed paths only, and a diff scan that refuses
   any added certification (``status: verified``, ``certified_by``, ``can_certify``).
2. **Reproduction**: re-ask each originating question ``repetitions`` times against the current
   context AND against the staged context, same model config, prompt, snapshot, MCP tools and mode.
   Outcome distributions are reported per side (no gold exists for real user questions, so the
   reviewer judges correctness; nothing is called an improvement from one lucky response).
3. **Regression**: the Phase 2 harness runs the evaluation cases whose refs touch the affected
   objects (plus a fixed sentinel sample) on both contexts, ``repetitions`` each. A case that is
   correct by majority on baseline but not on candidate is a REGRESSION. Any privacy or security
   violation fails validation. Results are summarised as counts for the reviewer; the improver LLM
   never sees them (no tuning against held-out data).
4. **Verdict**: VALIDATION_FAILED on any static problem, regression or violation; otherwise the
   proposal moves to AWAITING_REVIEW. With no model credentials the live steps are BLOCKED and the
   proposal stays BLOCKED (never silently promoted).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from contextforge.agent.factory import open_agent
from contextforge.agent.providers.base import ModelProvider, ProviderNotConfigured
from contextforge.agent.schemas import AgentConfig
from contextforge.config.settings import ContextMode, Settings
from contextforge.context.loader import load_context
from contextforge.eval.cases import load_cases
from contextforge.eval.harness import EvalBlocked, RunPlan, run_experiment
from contextforge.eval.store import EvalStore
from contextforge.product.audit import AuditLog
from contextforge.product.proposals.models import State
from contextforge.product.proposals.patches import PatchError, check_repo_path, stage
from contextforge.product.proposals.store import ProposalRow, ProposalStore

FORBIDDEN_ADDITIONS = ("status: verified", "certified_by", "can_certify", "certified_at")
SENTINEL_CASES = 6  # always-run dev cases so unrelated definitions are also checked


def static_checks(row: ProposalRow, settings: Settings, ctx_dir: Path, work: Path) -> tuple[list[str], Any]:
    problems: list[str] = []
    ops = list(row.proposal.draft.operations)
    if not ops:
        return ["proposal has no context operations (decision requests are not deployable)"], None
    try:
        staged = stage(
            repo_root=settings.root,
            context_dir=ctx_dir,
            serving_path=settings.serving_path,
            ops=ops,
            author=row.proposal.author,
            evidence_refs=[f"{e.kind}:{e.ref}" for e in row.proposal.draft.evidence][:10],
            work_dir=work,
        )
    except PatchError as exc:
        return [f"operations no longer apply to the current context: {exc}"], None
    problems += staged.validation_errors
    for d in staged.diffs:
        try:
            check_repo_path(d.path)
        except PatchError as exc:
            problems.append(str(exc))
        for line in d.unified_diff.splitlines():
            if (
                line.startswith("+")
                and not line.startswith("+++")
                and any(f in line for f in FORBIDDEN_ADDITIONS)
            ):
                problems.append(f"{d.path}: adds certification fields ({line[:120]})")
    if load_context(ctx_dir).version != row.proposal.base_context_version:
        staged.warnings.append(
            "base context changed since the proposal was drafted; re-staged on the current version"
        )
    return problems, staged


async def _reproduce(
    settings: Settings,
    cfg: AgentConfig,
    ctx: Path,
    question: str,
    reps: int,
    provider_factory: Callable[[], ModelProvider] | None,
    principal: str,
) -> list[dict[str, Any]]:
    out = []
    for _ in range(reps):
        prov = provider_factory() if provider_factory else None
        try:
            async with open_agent(
                settings,
                cfg,
                mode=ContextMode.full_context,
                provider=prov,
                context_dir=ctx,
                principal=principal,
            ) as agent:
                r = (await agent.ask(question)).response
        finally:
            if prov is not None:
                await prov.aclose()
        out.append(
            {
                "outcome": r.outcome.value,
                "executed_sql": r.executed_sql,
                "answer": r.answer[:500],
                "metrics": r.referenced_metrics,
                "terms": r.referenced_terms,
                "error": r.error.code if r.error else None,
                "context_version": r.context_version,
                "resolved_models": r.model.resolved_models,
            }
        )
    return out


def _majority(attempts: list[dict[str, Any]]) -> dict[str, bool]:
    by: dict[str, list[bool]] = defaultdict(list)
    for a in attempts:
        by[a["case_id"]].append(bool(a["correct"]))
    return {k: sum(v) * 2 > len(v) for k, v in by.items()}


def regression_cases(settings: Settings, affected: list[str]) -> list[str]:
    cases = load_cases(settings.root / "evals" / "cases")
    aff = set(affected)
    related = [
        c.id for c in cases if aff & set(c.refs.assets + c.refs.metrics + c.refs.terms + c.refs.unsupported)
    ]
    sentinel = [c.id for c in cases if c.split == "dev" and c.id not in related][:SENTINEL_CASES]
    return related + sentinel


class ProposalValidator:
    def __init__(
        self,
        settings: Settings,
        proposals: ProposalStore,
        audit: AuditLog,
        *,
        context_dir: Callable[[], Path],
        agent_config: AgentConfig,
        provider_factory: Callable[[], ModelProvider] | None,
        repetitions: int = 3,
        principal: str = "principal.local_analyst",
    ) -> None:
        self.settings = settings
        self.proposals = proposals
        self.audit = audit
        self.context_dir = context_dir
        self.cfg = agent_config
        self.provider_factory = provider_factory
        self.reps = repetitions
        self.principal = principal

    def validate(self, proposal_id: str) -> dict[str, Any]:
        row = self.proposals.get(proposal_id)
        if row is None:
            raise KeyError(proposal_id)
        self.proposals.transition(
            proposal_id, State.VALIDATING, actor="system.validator", reason="validation started"
        )
        ctx = self.context_dir()
        report: dict[str, Any] = {
            "started_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "revision": row.revision,
            "repetitions": self.reps,
            "agent_config": self.cfg.model_dump(mode="json"),
        }
        with tempfile.TemporaryDirectory(prefix="cf-validate-") as t:
            problems, staged = static_checks(row, self.settings, ctx, Path(t) / "repo")
            report["static"] = {
                "problems": problems,
                "warnings": staged.warnings if staged else [],
                "files": staged.changed_files if staged else [],
                "proposed_context_version": staged.proposed_version if staged else None,
            }
            if problems:
                return self._finish(proposal_id, State.VALIDATION_FAILED, report, "static checks failed")
            try:
                report["reproduction"] = self._run_reproduction(row, ctx, staged.staged_dir)
                report["regression"] = self._run_regression(row, ctx, staged.staged_dir)
            except (ProviderNotConfigured, EvalBlocked) as exc:
                report["blocked"] = str(getattr(exc, "message", exc))
                return self._finish(proposal_id, State.BLOCKED, report, "live evaluation BLOCKED")
        reg = report["regression"]
        failed = (
            bool(reg["regressed_cases"])
            or reg["privacy_violations"]
            or reg["security_violations"]
            or reg["incomplete"]
        )
        if failed:
            return self._finish(
                proposal_id, State.VALIDATION_FAILED, report, "regression or violation detected"
            )
        return self._finish(
            proposal_id,
            State.AWAITING_REVIEW,
            report,
            "validation passed",
            proposed=report["static"]["proposed_context_version"],
        )

    def _run_reproduction(self, row: ProposalRow, base: Path, cand: Path) -> dict[str, Any]:
        out = {}
        questions = []
        for tid in row.proposal.trace_ids[:3]:
            r = self.proposals.db.one("SELECT question FROM requests WHERE request_id = ?", (tid,))
            if r:
                questions.append(str(r[0]))
        for q in questions:
            before = asyncio.run(
                _reproduce(self.settings, self.cfg, base, q, self.reps, self.provider_factory, self.principal)
            )
            after = asyncio.run(
                _reproduce(self.settings, self.cfg, cand, q, self.reps, self.provider_factory, self.principal)
            )
            out[q] = {
                "before": before,
                "after": after,
                "before_outcomes": dict(Counter(a["outcome"] for a in before)),
                "after_outcomes": dict(Counter(a["outcome"] for a in after)),
            }
        return {
            "questions": out,
            "note": "no gold exists for real user questions; the reviewer judges correctness",
        }

    def _run_regression(self, row: ProposalRow, base: Path, cand: Path) -> dict[str, Any]:
        case_ids = regression_cases(self.settings, row.proposal.affected_ids)
        if self.provider_factory is None:
            raise ProviderNotConfigured("no model provider configured for validation", missing=["provider"])
        res: dict[str, Any] = {"case_ids": case_ids}
        majorities = {}
        all_attempts: dict[str, list[dict[str, Any]]] = {}
        for side, ctx in (("baseline", base), ("candidate", cand)):
            s = self.settings.model_copy(update={"context_dir": ctx})
            plan = RunPlan(
                modes=[ContextMode.full_context],
                split="all",
                case_ids=case_ids,
                repetitions=self.reps,
                concurrency=2,
                kind="regression",
                label=f"proposal:{row.proposal_id}:r{row.revision}:{side}",
            )
            prov = self.provider_factory()
            try:
                run_id = asyncio.run(run_experiment(s, plan, self.cfg, prov))
            finally:
                asyncio.run(prov.aclose())
            from contextforge.eval.harness import eval_paths

            store = EvalStore(eval_paths(self.settings)[0])
            run = store.run(run_id) or {}
            atts = store.attempts(run_id)
            store.close()
            res[f"{side}_run_id"] = run_id
            res[f"{side}_status"] = run.get("status")
            majorities[side] = _majority(atts)
            all_attempts[side] = atts
        b, c = majorities["baseline"], majorities["candidate"]
        res["regressed_cases"] = sorted(k for k in b if b[k] and not c.get(k, False))
        res["improved_cases"] = sorted(k for k in c if c[k] and not b.get(k, False))
        res["baseline_correct"] = sum(b.values())
        res["candidate_correct"] = sum(c.values())
        res["cases"] = len(b)
        res["privacy_violations"] = sum(
            len(a["score"].get("privacy_violations", [])) for a in all_attempts["candidate"]
        )
        res["security_violations"] = sum(
            len(a["score"].get("security_violations", [])) for a in all_attempts["candidate"]
        )
        res["incomplete"] = any(res.get(f"{s}_status") != "COMPLETE" for s in ("baseline", "candidate"))
        return res

    def _finish(
        self, proposal_id: str, to: State, report: dict[str, Any], reason: str, proposed: str | None = None
    ) -> dict[str, Any]:
        report["finished_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        report["verdict"] = to.value
        updates: dict[str, Any] = {"validation_json": report}
        if proposed:
            updates["proposed_context_version"] = proposed
        self.proposals.transition(proposal_id, to, actor="system.validator", reason=reason, updates=updates)
        self.audit.append(
            action="validation.performed",
            actor_id="system.validator",
            actor_type="system",
            resource_type="proposal",
            resource_id=proposal_id,
            new_state=to.value,
            details={k: report.get(k) for k in ("verdict", "static", "blocked")}
            | {
                "regressed": (report.get("regression") or {}).get("regressed_cases"),
                "improved": (report.get("regression") or {}).get("improved_cases"),
            },
        )
        if to == State.VALIDATION_FAILED and (report.get("regression") or {}).get("regressed_cases"):
            self.audit.append(
                action="regression.detected",
                actor_id="system.validator",
                actor_type="system",
                resource_type="proposal",
                resource_id=proposal_id,
                details={"cases": report["regression"]["regressed_cases"]},
            )
        return report
