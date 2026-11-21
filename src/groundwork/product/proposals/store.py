"""Persistent proposal lifecycle with explicit, idempotent, audited transitions.

Every transition runs inside one IMMEDIATE SQLite transaction that (1) re-reads the current state,
(2) checks the edge against ``TRANSITIONS``, (3) writes the new state and (4) appends the audit
event. Two reviewers clicking at the same time are therefore serialised: the second sees the
already-decided state and is refused (and audited), never applied.

Human decisions (``decide``) additionally require, on the backend:
* the actor is a configured context reviewer mapped to a non-placeholder human owner;
* the reviewer covers every affected domain, and is ``elevated`` for elevated-tier proposals;
* the decision targets the CURRENT revision (stale approvals are refused);
* the reviewer is not the proposal author (no self-approval; the AI author can never approve).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from groundwork.product.audit import AuditLog
from groundwork.product.config import ProductConfig, reviewer_covers
from groundwork.product.db import Database
from groundwork.product.proposals.models import TRANSITIONS, ContextProposal, State


class TransitionError(ValueError):
    pass


class DecisionRefused(PermissionError):
    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def signature_for(category: str, affected_ids: list[str], question: str) -> str:
    """Stable dedupe key: the same failure retried (or reported twice) maps to one proposal."""
    basis = {"category": category, "ids": sorted(set(affected_ids))}
    if not affected_ids:
        basis["question"] = " ".join(question.lower().split())
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


@dataclass
class ProposalRow:
    proposal_id: str
    state: State
    revision: int
    review_tier: str
    proposal: ContextProposal
    validation: dict[str, Any] | None
    raw: dict[str, Any]


class ProposalStore:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    # ---------------- creation / revision ----------------

    def create(
        self, proposal: ContextProposal, signature: str, *, state: State = State.PROPOSED
    ) -> tuple[ProposalRow, bool]:
        """Insert or return the existing proposal for ``signature``. Returns (row, created)."""
        with self.db.tx() as c:
            existing = c.execute(
                "SELECT proposal_id FROM proposals WHERE signature = ?", (signature,)
            ).fetchone()
            if existing:
                pid = existing[0]
                # merge provenance of the new occurrence (traces/failures) without a new proposal
                row = c.execute(
                    "SELECT trace_ids, failure_ids FROM proposals WHERE proposal_id = ?", (pid,)
                ).fetchone()
                traces = list(dict.fromkeys([*json.loads(row[0]), *proposal.trace_ids]))
                fails = list(dict.fromkeys([*json.loads(row[1]), *proposal.failure_ids]))
                c.execute(
                    "UPDATE proposals SET trace_ids = ?, failure_ids = ?, updated_at = ? WHERE proposal_id = ?",
                    (json.dumps(traces), json.dumps(fails), _now(), pid),
                )
                created = False
            else:
                pid = proposal.proposal_id
                now = _now()
                c.execute(
                    """INSERT INTO proposals (proposal_id, signature, state, revision, category, failure_ids,
                       trace_ids, eval_case_ids, title, body_json, review_tier, base_context_version,
                       proposed_context_version, author, author_type, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        pid,
                        signature,
                        state.value,
                        proposal.revision,
                        proposal.category,
                        json.dumps(proposal.failure_ids),
                        json.dumps(proposal.trace_ids),
                        json.dumps(proposal.eval_case_ids),
                        proposal.title,
                        json.dumps(proposal.body()),
                        proposal.review_tier,
                        proposal.base_context_version,
                        proposal.proposed_context_version,
                        proposal.author,
                        proposal.author_type,
                        now,
                        now,
                    ),
                )
                self.audit.append(
                    action="proposal.created",
                    actor_id=proposal.author,
                    actor_type=proposal.author_type,
                    resource_type="proposal",
                    resource_id=pid,
                    new_state=state.value,
                    trace_id=proposal.trace_ids[0] if proposal.trace_ids else None,
                    context_version=proposal.base_context_version,
                    details={
                        "revision": proposal.revision,
                        "tier": proposal.review_tier,
                        "files": [d.path for d in proposal.diffs],
                    },
                    conn=c,
                )
                created = True
        row_ = self.get(pid)
        assert row_ is not None
        return row_, created

    def revise(
        self, proposal_id: str, new: ContextProposal, *, actor: str, actor_type: Literal["human", "agent"]
    ) -> ProposalRow:
        with self.db.tx() as c:
            cur = self._locked(c, proposal_id)
            state = State(cur["state"])
            if State.PROPOSED not in TRANSITIONS[state]:
                raise TransitionError(f"cannot revise a proposal in state {state}")
            rev = int(cur["revision"]) + 1
            body = new.model_copy(update={"revision": rev}).body()
            c.execute(
                """UPDATE proposals SET revision = ?, body_json = ?, review_tier = ?, state = ?,
                   proposed_context_version = ?, validation_json = NULL, decision = NULL, decided_by = NULL,
                   decision_reason = NULL, decided_at = NULL, decided_revision = NULL, updated_at = ?
                   WHERE proposal_id = ?""",
                (
                    rev,
                    json.dumps(body),
                    new.review_tier,
                    State.PROPOSED.value,
                    new.proposed_context_version,
                    _now(),
                    proposal_id,
                ),
            )
            self.audit.append(
                action="proposal.revised",
                actor_id=actor,
                actor_type=actor_type,
                resource_type="proposal",
                resource_id=proposal_id,
                prior_state=state.value,
                new_state=State.PROPOSED.value,
                details={"revision": rev, "note": new.revision_note},
                conn=c,
            )
        out = self.get(proposal_id)
        assert out is not None
        return out

    # ---------------- reads ----------------

    def get(self, proposal_id: str) -> ProposalRow | None:
        r = self.db.one("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,))
        return self._row(dict(r)) if r else None

    def list(self, states: list[State] | None = None) -> list[ProposalRow]:
        rows = self.db.query("SELECT * FROM proposals ORDER BY created_at DESC")
        out = [self._row(dict(r)) for r in rows]
        return [p for p in out if states is None or p.state in states]

    def _row(self, d: dict[str, Any]) -> ProposalRow:
        return ProposalRow(
            proposal_id=d["proposal_id"],
            state=State(d["state"]),
            revision=int(d["revision"]),
            review_tier=d["review_tier"],
            proposal=ContextProposal.model_validate(json.loads(d["body_json"])),
            validation=json.loads(d["validation_json"]) if d["validation_json"] else None,
            raw=d,
        )

    @staticmethod
    def _locked(c: Any, proposal_id: str) -> dict[str, Any]:
        r = c.execute("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        if r is None:
            raise TransitionError(f"unknown proposal {proposal_id}")
        return dict(r)

    # ---------------- transitions ----------------

    def transition(
        self,
        proposal_id: str,
        to: State,
        *,
        actor: str,
        actor_type: Literal["human", "system", "agent"] = "system",
        reason: str | None = None,
        updates: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        github_ref: str | None = None,
    ) -> bool:
        """Apply one edge. Returns False (no-op) if already in ``to``. Raises on an illegal edge."""
        allowed_cols = {
            "validation_json",
            "pr_number",
            "pr_url",
            "branch",
            "head_sha",
            "merge_commit",
            "review_message_ts",
            "proposed_context_version",
        }
        with self.db.tx() as c:
            cur = self._locked(c, proposal_id)
            state = State(cur["state"])
            if expected_revision is not None and int(cur["revision"]) != expected_revision:
                raise TransitionError(f"stale revision {expected_revision}; current is {cur['revision']}")
            if state == to:
                if updates:
                    self._update(c, proposal_id, updates, allowed_cols)
                return False
            if to not in TRANSITIONS[state]:
                raise TransitionError(f"illegal transition {state} -> {to}")
            if to == State.APPROVED:
                raise TransitionError("APPROVED can only be reached through decide() by an authorized human")
            c.execute(
                "UPDATE proposals SET state = ?, updated_at = ? WHERE proposal_id = ?",
                (to.value, _now(), proposal_id),
            )
            if updates:
                self._update(c, proposal_id, updates, allowed_cols)
            self.audit.append(
                action="proposal.transition",
                actor_id=actor,
                actor_type=actor_type,
                resource_type="proposal",
                resource_id=proposal_id,
                prior_state=state.value,
                new_state=to.value,
                github_ref=github_ref,
                context_version=cur["base_context_version"],
                details={"reason": reason, "revision": cur["revision"]},
                conn=c,
            )
        return True

    @staticmethod
    def _update(c: Any, proposal_id: str, updates: dict[str, Any], allowed: set[str]) -> None:
        bad = set(updates) - allowed
        if bad:
            raise TransitionError(f"columns not updatable through transition(): {bad}")
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = [json.dumps(v) if isinstance(v, dict | list) else v for v in updates.values()]
        c.execute(f"UPDATE proposals SET {sets} WHERE proposal_id = ?", (*vals, proposal_id))

    def decide(
        self,
        proposal_id: str,
        *,
        decision: Literal["approve", "reject", "request_changes"],
        slack_user_id: str,
        revision: int,
        config: ProductConfig,
        reason: str | None = None,
        slack_event_id: str | None = None,
    ) -> State:
        """Record an authorized human decision. Raises DecisionRefused (and audits it) otherwise."""
        reviewer = config.context_reviewer(slack_user_id)
        with self.db.tx() as c:
            cur = self._locked(c, proposal_id)
            state = State(cur["state"])
            body = ContextProposal.model_validate(json.loads(cur["body_json"]))

            def refuse(code: str, why: str) -> DecisionRefused:
                self.audit.append(
                    action="approval.denied_stale"
                    if code in {"stale_revision", "not_awaiting_review"}
                    else "approval.denied_unauthorized",
                    actor_id=f"slack:{slack_user_id}",
                    actor_type="human",
                    resource_type="proposal",
                    resource_id=proposal_id,
                    prior_state=state.value,
                    new_state=state.value,
                    slack_event_id=slack_event_id,
                    details={"decision": decision, "code": code, "reason": why, "revision": revision},
                    conn=c,
                )
                return DecisionRefused(why, code)

            if reviewer is None:
                err = refuse("not_a_reviewer", "you are not a configured context reviewer")
            elif reviewer.owner_id == cur["author"] or f"slack:{slack_user_id}" == cur["author"]:
                err = refuse("self_approval", "the author of a proposal cannot review it")
            elif not reviewer_covers(reviewer, body.domains, elevated=cur["review_tier"] == "elevated"):
                err = refuse(
                    "insufficient_scope",
                    "this proposal needs an elevated reviewer"
                    if cur["review_tier"] == "elevated" and not reviewer.elevated
                    else f"you are not a reviewer for domains {body.domains}",
                )
            elif state != State.AWAITING_REVIEW:
                err = refuse("not_awaiting_review", f"proposal is {state.value}; nothing to decide")
            elif int(cur["revision"]) != revision:
                err = refuse(
                    "stale_revision",
                    f"this review card is for revision {revision}; current is {cur['revision']}",
                )
            else:
                err = None
            if err is not None:
                # commit the refusal audit event, then raise outside the transaction
                pass
            else:
                assert reviewer is not None
                to = {
                    "approve": State.APPROVED,
                    "reject": State.REJECTED,
                    "request_changes": State.CHANGES_REQUESTED,
                }[decision]
                c.execute(
                    """UPDATE proposals SET state = ?, decision = ?, decided_by = ?, decision_reason = ?,
                       decided_at = ?, decided_revision = ?, updated_at = ? WHERE proposal_id = ?""",
                    (to.value, decision, reviewer.owner_id, reason, _now(), revision, _now(), proposal_id),
                )
                self.audit.append(
                    action={
                        "approve": "approval.granted",
                        "reject": "approval.rejected",
                        "request_changes": "revision.requested",
                    }[decision],
                    actor_id=reviewer.owner_id,
                    actor_type="human",
                    resource_type="proposal",
                    resource_id=proposal_id,
                    prior_state=state.value,
                    new_state=to.value,
                    github_ref=cur["pr_url"],
                    slack_event_id=slack_event_id,
                    context_version=cur["base_context_version"],
                    details={"revision": revision, "reason": reason, "slack_user_id": slack_user_id},
                    conn=c,
                )
        if err is not None:
            raise err
        return to
