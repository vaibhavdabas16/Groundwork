"""Phase 3 unit tests: audit chain, failure taxonomy, proposal lifecycle + authorization, config
validation, Block Kit rendering. No Slack, GitHub or model calls."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from groundwork.agent.schemas import (
    AgentResponse,
    CostInfo,
    ErrorDetail,
    ModelInfo,
    Outcome,
    QueryRecord,
    ToolCallRecord,
)
from groundwork.context.loader import load_context
from groundwork.product import blocks
from groundwork.product.audit import AuditLog
from groundwork.product.config import ContextReviewer, ProductConfig, check_against_context
from groundwork.product.db import Database
from groundwork.product.failures import FailureCategory as C
from groundwork.product.failures import classify
from groundwork.product.proposals.models import ContextProposal, ProposalDraft, State
from groundwork.product.proposals.store import (
    DecisionRefused,
    ProposalStore,
    TransitionError,
    signature_for,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "ops.sqlite")


@pytest.fixture(scope="module")
def store() -> Any:
    return load_context(ROOT / "context")


# ---------------- audit ----------------


def test_audit_is_append_only_and_chain_detects_tampering(db: Database) -> None:
    log = AuditLog(db)
    for i in range(3):
        log.append(
            action="question.received",
            actor_id="slack:U1",
            actor_type="human",
            resource_type="request",
            resource_id=f"r{i}",
        )
    assert log.verify().ok and log.verify().events == 3
    head = log.head()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), db.tx() as c:
        c.execute("UPDATE audit_events SET actor_id = 'x' WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), db.tx() as c:
        c.execute("DELETE FROM audit_events WHERE seq = 2")
    # an attacker/operator who drops the triggers and edits a row is still detected
    raw = sqlite3.connect(db.path, isolation_level=None)
    raw.execute("DROP TRIGGER audit_no_update")
    raw.execute("UPDATE audit_events SET actor_id = 'forged' WHERE seq = 2")
    rep = log.verify()
    assert not rep.ok and rep.at_seq == 2 and "modified" in (rep.problem or "")
    raw.execute("DROP TRIGGER audit_no_delete")
    raw.execute("UPDATE audit_events SET actor_id = 'slack:U1' WHERE seq = 2")
    raw.execute("DELETE FROM audit_events WHERE seq = 3")
    assert log.verify(expected_head=head).problem is not None  # truncation vs exported head


def test_audit_rejects_unknown_actions_and_actor_types(db: Database) -> None:
    log = AuditLog(db)
    with pytest.raises(ValueError):
        log.append(action="made.up", actor_id="x", actor_type="system", resource_type="r", resource_id="1")
    with pytest.raises(sqlite3.IntegrityError):
        log.append(
            action="question.received", actor_id="x", actor_type="robot", resource_type="r", resource_id="1"
        )  # type: ignore[arg-type]


# ---------------- failure taxonomy ----------------


def _resp(
    outcome: Outcome,
    *,
    err: str | None = None,
    retryable: bool = False,
    qcodes: list[str] | None = None,
    ids: list[str] | None = None,
    cert: str = "none",
) -> AgentResponse:
    return AgentResponse(
        request_id="t1",
        trace_id="t1",
        outcome=outcome,
        question="q",
        answer="a",
        context_mode="full_context",
        certification_status=cert,  # type: ignore[arg-type]
        queries=[
            QueryRecord(query_id=f"q{i}", generated_sql="select 1", executed_sql=None, ok=False, error_code=c)
            for i, c in enumerate(qcodes or [])
        ],
        tool_calls=[
            ToolCallRecord(step=1, tool="get_definition", arguments={"name": i}, ok=True, summary={"id": i})
            for i in ids or []
        ],
        model=ModelInfo(provider="x", requested_model="m", live=True),
        cost=CostInfo(estimated_usd=None, pricing_version="v"),
        latency_ms=1.0,
        error=ErrorDetail(code=err, message="m", stage="s", retryable=retryable) if err else None,
        prompt_version="p",
        prompt_hash="h",
    )


@pytest.mark.parametrize(
    ("resp_kw", "category", "fixable"),
    [
        (
            {"outcome": Outcome.EXECUTION_ERROR, "err": "provider_rate_limited", "retryable": True},
            C.MODEL_UNAVAILABLE,
            False,
        ),
        ({"outcome": Outcome.EXECUTION_ERROR, "err": "provider_auth"}, C.MODEL_UNAVAILABLE, False),
        (
            {"outcome": Outcome.EXECUTION_ERROR, "err": "context_source_unavailable"},
            C.CONTEXT_RETRIEVAL_FAILURE,
            False,
        ),
        (
            {"outcome": Outcome.EXECUTION_ERROR, "qcodes": ["execution.warehouse_unavailable"]},
            C.DATABASE_UNAVAILABLE,
            False,
        ),
        (
            {"outcome": Outcome.EXECUTION_ERROR, "qcodes": ["execution.timeout"]},
            C.QUERY_RESOURCE_LIMIT,
            False,
        ),
        (
            {"outcome": Outcome.EXECUTION_ERROR, "qcodes": ["execution.cost_limit_exceeded"]},
            C.QUERY_RESOURCE_LIMIT,
            False,
        ),
        (
            {"outcome": Outcome.POLICY_DENIED, "qcodes": ["sql_rejected.relation_not_allowed"]},
            C.PERMISSION_DENIED,
            False,
        ),
        (
            {
                "outcome": Outcome.EXECUTION_ERROR,
                "qcodes": ["sql_rejected.parse_error"] * 3,
                "err": "sql_repair_budget_exhausted",
            },
            C.SQL_GENERATION_FAILURE,
            False,
        ),
        (
            {"outcome": Outcome.EXECUTION_ERROR, "qcodes": ["execution.execution_error"]},
            C.SQL_EXECUTION_FAILURE,
            False,
        ),
        ({"outcome": Outcome.CLARIFICATION_REQUIRED, "ids": ["unsupported.gmv"]}, C.AMBIGUOUS_METRIC, True),
        (
            {"outcome": Outcome.INSUFFICIENT_CONTEXT, "ids": ["unsupported.net_revenue"]},
            C.INSUFFICIENT_SOURCE_DATA,
            True,
        ),
        ({"outcome": Outcome.INSUFFICIENT_CONTEXT, "ids": []}, C.MISSING_BUSINESS_DEFINITION, True),
        ({"outcome": Outcome.ANSWER, "cert": "deprecated_used"}, C.DEPRECATED_ASSET_USAGE, True),
    ],
)
def test_classification_distinguishes_context_gaps_from_infrastructure(
    store: Any, resp_kw: dict[str, Any], category: C, fixable: bool
) -> None:
    rec = classify(_resp(**resp_kw), store)
    assert rec is not None and rec.category == category
    assert rec.context_fix_appropriate is fixable
    if category in (C.AMBIGUOUS_METRIC, C.INSUFFICIENT_SOURCE_DATA):
        assert rec.fix_kind == "human_decision"  # never auto-generated business policy
    assert "Olist" not in rec.safe_explanation and rec.trace_id == "t1"


def test_successful_answer_and_plain_clarification_are_not_failures(store: Any) -> None:
    assert classify(_resp(Outcome.ANSWER, cert="draft"), store) is None
    assert classify(_resp(Outcome.CLARIFICATION_REQUIRED), store) is None


# ---------------- proposal lifecycle and authorization ----------------


def _proposal(
    pid: str = "cfp-test",
    author: str = "agent.groundwork_improver",
    tier: str = "standard",
    domains: list[str] | None = None,
) -> ContextProposal:
    return ContextProposal(
        proposal_id=pid,
        revision=1,
        category="MISSING_COLUMN_DESCRIPTION",
        title="Describe customer_unique_key",
        draft=ProposalDraft(
            kind="context_change",
            summary="Describe the person-level key",
            deficiency="agent counted customer_key",
        ),
        affected_ids=["asset.analytics.customers"],
        domains=domains or ["commerce.customers"],
        review_tier=tier,  # type: ignore[arg-type]
        diffs=[],
        base_context_version="ctx-a",
        proposed_context_version="ctx-b",
        author=author,
        author_type="agent",
        trace_ids=["t1"],
        failure_ids=["f1"],
        eval_case_ids=[],
        created_at="2026-11-20T00:00:00+00:00",
    )


def _cfg(**kw: Any) -> ProductConfig:
    return ProductConfig(
        context_reviewers=[
            ContextReviewer(slack_user_id="UREVIEW1", owner_id="person.reviewer", domains=["commerce.*"]),
            ContextReviewer(
                slack_user_id="UELEV01", owner_id="person.elevated", domains=["*"], elevated=True
            ),
            ContextReviewer(slack_user_id="ULOGIST", owner_id="person.logistics", domains=["logistics.*"]),
        ],
        **kw,
    )


def _to_review(ps: ProposalStore, pid: str) -> None:
    ps.transition(pid, State.VALIDATING, actor="system.validator")
    ps.transition(pid, State.AWAITING_REVIEW, actor="system.validator")


def test_lifecycle_rejects_illegal_edges_and_approval_without_human(db: Database) -> None:
    ps = ProposalStore(db, AuditLog(db))
    row, created = ps.create(_proposal(), "sig1")
    assert created and row.state == State.PROPOSED
    with pytest.raises(TransitionError):
        ps.transition("cfp-test", State.MERGED, actor="system")
    _to_review(ps, "cfp-test")
    with pytest.raises(TransitionError, match="authorized human"):
        ps.transition("cfp-test", State.APPROVED, actor="agent.groundwork_improver", actor_type="agent")
    assert ps.transition("cfp-test", State.AWAITING_REVIEW, actor="system") is False  # idempotent no-op
    again, created2 = ps.create(_proposal(pid="cfp-other"), "sig1")
    assert not created2 and again.proposal_id == "cfp-test"  # same failure signature -> no duplicate


def test_decisions_enforce_reviewer_scope_revision_and_no_self_approval(db: Database) -> None:
    audit = AuditLog(db)
    ps = ProposalStore(db, audit)
    cfg = _cfg()
    ps.create(_proposal(), "s1")
    _to_review(ps, "cfp-test")
    for uid, code in (("UNOBODY", "not_a_reviewer"), ("ULOGIST", "insufficient_scope")):
        with pytest.raises(DecisionRefused) as ei:
            ps.decide("cfp-test", decision="approve", slack_user_id=uid, revision=1, config=cfg)
        assert ei.value.code == code
    with pytest.raises(DecisionRefused) as ei:
        ps.decide("cfp-test", decision="approve", slack_user_id="UREVIEW1", revision=0, config=cfg)
    assert ei.value.code == "stale_revision"
    assert (
        ps.decide("cfp-test", decision="approve", slack_user_id="UREVIEW1", revision=1, config=cfg)
        == State.APPROVED
    )
    with pytest.raises(DecisionRefused) as ei:  # replayed click / second reviewer after decision
        ps.decide("cfp-test", decision="reject", slack_user_id="UELEV01", revision=1, config=cfg)
    assert ei.value.code == "not_awaiting_review"
    denied = [e for e in audit.events(resource_id="cfp-test") if e["action"].startswith("approval.denied")]
    assert len(denied) == 4 and audit.verify().ok
    # self-approval: a reviewer who authored the proposal
    ps.create(_proposal(pid="cfp-self", author="person.reviewer"), "s2")
    _to_review(ps, "cfp-self")
    with pytest.raises(DecisionRefused) as ei:
        ps.decide("cfp-self", decision="approve", slack_user_id="UREVIEW1", revision=1, config=cfg)
    assert ei.value.code == "self_approval"
    # elevated tier needs an elevated reviewer
    ps.create(_proposal(pid="cfp-elev", tier="elevated"), "s3")
    _to_review(ps, "cfp-elev")
    with pytest.raises(DecisionRefused):
        ps.decide("cfp-elev", decision="approve", slack_user_id="UREVIEW1", revision=1, config=cfg)
    assert (
        ps.decide("cfp-elev", decision="approve", slack_user_id="UELEV01", revision=1, config=cfg)
        == State.APPROVED
    )


def test_concurrent_conflicting_decisions_apply_exactly_once(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.sqlite")
    ps = ProposalStore(db, AuditLog(db))
    cfg = _cfg()
    ps.create(_proposal(), "s1")
    _to_review(ps, "cfp-test")
    results: list[str] = []

    def act(uid: str, decision: str) -> None:
        local = ProposalStore(Database(tmp_path / "ops.sqlite"), AuditLog(Database(tmp_path / "ops.sqlite")))
        try:
            results.append(
                local.decide("cfp-test", decision=decision, slack_user_id=uid, revision=1, config=cfg).value
            )  # type: ignore[arg-type]
        except DecisionRefused as exc:
            results.append(exc.code)

    ts = [threading.Thread(target=act, args=a) for a in (("UREVIEW1", "approve"), ("UELEV01", "reject")) * 3]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    applied = [r for r in results if r in ("APPROVED", "REJECTED")]
    assert len(applied) == 1 and results.count("not_awaiting_review") == 5


def test_revision_invalidates_prior_review_card(db: Database) -> None:
    ps = ProposalStore(db, AuditLog(db))
    cfg = _cfg()
    ps.create(_proposal(), "s1")
    _to_review(ps, "cfp-test")
    ps.decide(
        "cfp-test",
        decision="request_changes",
        slack_user_id="UREVIEW1",
        revision=1,
        config=cfg,
        reason="cite schema",
    )
    row = ps.revise("cfp-test", _proposal(), actor="person.reviewer", actor_type="human")
    assert row.revision == 2 and row.state == State.PROPOSED and row.raw["decided_by"] is None
    _to_review(ps, "cfp-test")
    with pytest.raises(DecisionRefused) as ei:
        ps.decide("cfp-test", decision="approve", slack_user_id="UREVIEW1", revision=1, config=cfg)
    assert ei.value.code == "stale_revision"


def test_signature_is_stable_and_order_independent() -> None:
    assert signature_for("X", ["b", "a"], "q1") == signature_for("X", ["a", "b"], "q2")
    assert signature_for("X", [], "How many  orders?") == signature_for("X", [], "how many orders?")


def test_config_rejects_placeholder_or_agent_reviewers(store: Any) -> None:
    cfg = ProductConfig(
        context_reviewers=[ContextReviewer(slack_user_id="UX123", owner_id="team.commerce_analytics")]
    )
    assert any("non-placeholder human" in p for p in check_against_context(cfg, store))
    cfg2 = ProductConfig(
        context_reviewers=[ContextReviewer(slack_user_id="UX123", owner_id="agent.claude_code")]
    )
    assert check_against_context(cfg2, store)
    cfg3 = ProductConfig.model_validate(
        {"users": [{"slack_user_id": "UX123", "principal": "principal.root"}]}
    )
    assert any("unknown principal" in p for p in check_against_context(cfg3, store))


# ---------------- Block Kit ----------------


def test_answer_blocks_escape_model_text_redact_and_respect_limits(store: Any) -> None:
    r = _resp(Outcome.ANSWER, cert="draft").model_copy(
        update={
            "answer": "<!channel> total is 5 <https://evil|click>",
            "executed_sql": "SELECT 1",
            "result_columns": ["email", "n"],
            "result_preview": [["a@b.com", 1.5]] * 30,
            "result_row_count": 30,
            "referenced_metrics": ["metric.order_count"],
        }
    )
    bl = blocks.answer_blocks(r, store, request_id="rid1")
    text = json.dumps(bl)
    assert "<!channel>" not in text and "<https://evil" not in text
    assert "a@b.com" not in text and "[redacted]" in text
    assert "DRAFT" in text and "rid1" in text and len(bl) <= 50
    assert all(len(b.get("text", {}).get("text", "")) <= 3000 for b in bl)
    assert "Showing 10 of 30 rows" in text
