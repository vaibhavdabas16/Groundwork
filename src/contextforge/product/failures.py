"""Failure taxonomy and deterministic classification of unsuccessful requests.

Classification uses ONLY recorded evidence (the structured ``AgentResponse``: outcome, error code,
query error codes, tool-call results, referenced context ids) plus the governed context store. It
never asks an LLM whether something is a context problem.

The decisive output is ``context_fix_appropriate``:
* infrastructure / provider / permission failures are NEVER context problems (a database outage
  must not produce a glossary edit; a rate limit must not produce a metric);
* missing source data is documented as unsupported, never "fixed" by inventing a calculation;
* ambiguous concepts that need a business decision produce a HUMAN-DECISION request, not text.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from contextforge.agent.schemas import AgentResponse, Outcome
from contextforge.context.loader import ContextStore


class FailureCategory(StrEnum):
    MISSING_BUSINESS_DEFINITION = "MISSING_BUSINESS_DEFINITION"
    AMBIGUOUS_METRIC = "AMBIGUOUS_METRIC"
    MISSING_COLUMN_DESCRIPTION = "MISSING_COLUMN_DESCRIPTION"
    MISSING_JOIN_GUIDANCE = "MISSING_JOIN_GUIDANCE"
    INCORRECT_JOIN_GRAIN = "INCORRECT_JOIN_GRAIN"
    DEPRECATED_ASSET_USAGE = "DEPRECATED_ASSET_USAGE"
    CONTRADICTORY_CONTEXT = "CONTRADICTORY_CONTEXT"
    INSUFFICIENT_SOURCE_DATA = "INSUFFICIENT_SOURCE_DATA"
    SQL_GENERATION_FAILURE = "SQL_GENERATION_FAILURE"
    SQL_EXECUTION_FAILURE = "SQL_EXECUTION_FAILURE"
    QUERY_RESOURCE_LIMIT = "QUERY_RESOURCE_LIMIT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    CONTEXT_RETRIEVAL_FAILURE = "CONTEXT_RETRIEVAL_FAILURE"
    EVALUATION_REGRESSION = "EVALUATION_REGRESSION"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


C = FailureCategory

# category -> (remediation category, context fix appropriate?, "human_decision" | "proposal" | None)
REMEDIATION: dict[FailureCategory, tuple[str, str | None]] = {
    C.MISSING_BUSINESS_DEFINITION: ("context.add_definition_or_alias", "proposal"),
    C.AMBIGUOUS_METRIC: ("governance.decide_definition", "human_decision"),
    C.MISSING_COLUMN_DESCRIPTION: ("context.describe_column", "proposal"),
    C.MISSING_JOIN_GUIDANCE: ("context.document_join", "proposal"),
    C.INCORRECT_JOIN_GRAIN: ("context.fanout_warning", "proposal"),
    C.DEPRECATED_ASSET_USAGE: ("context.strengthen_deprecation", "proposal"),
    C.CONTRADICTORY_CONTEXT: ("governance.resolve_contradiction", "human_decision"),
    C.INSUFFICIENT_SOURCE_DATA: ("context.document_unsupported_and_source_requirement", "human_decision"),
    C.SQL_GENERATION_FAILURE: ("agent.investigate_prompt_or_model", None),
    C.SQL_EXECUTION_FAILURE: ("agent.investigate_sql", None),
    C.QUERY_RESOURCE_LIMIT: ("user.narrow_question_or_ops.review_limits", None),
    C.PERMISSION_DENIED: ("access.request_through_policy_owner", None),
    C.DATABASE_UNAVAILABLE: ("ops.restore_warehouse", None),
    C.MODEL_UNAVAILABLE: ("ops.provider_status_or_credentials", None),
    C.CONTEXT_RETRIEVAL_FAILURE: ("ops.restore_mcp_or_context", None),
    C.EVALUATION_REGRESSION: ("eval.investigate_regression", None),
    C.UNKNOWN_ERROR: ("ops.investigate", None),
}

SAFE_TEXT: dict[FailureCategory, str] = {
    C.MISSING_BUSINESS_DEFINITION: "I couldn't find a governed definition for part of this question.",
    C.AMBIGUOUS_METRIC: "This term has several possible definitions and no decision on which one to use.",
    C.MISSING_COLUMN_DESCRIPTION: "The data needed is not documented well enough to use safely.",
    C.MISSING_JOIN_GUIDANCE: "There is no documented way to combine the tables this needs.",
    C.INCORRECT_JOIN_GRAIN: "Combining these tables could double-count rows.",
    C.DEPRECATED_ASSET_USAGE: "The answer relied on a deprecated definition or table.",
    C.CONTRADICTORY_CONTEXT: "The governed definitions for this question contradict each other.",
    C.INSUFFICIENT_SOURCE_DATA: "The data needed to answer this is not available in the warehouse.",
    C.SQL_GENERATION_FAILURE: "I couldn't build a valid query for this question.",
    C.SQL_EXECUTION_FAILURE: "The query failed to run.",
    C.QUERY_RESOURCE_LIMIT: "The query was stopped because it exceeded resource limits. Try narrowing it.",
    C.PERMISSION_DENIED: "You don't have access to the data this question needs.",
    C.DATABASE_UNAVAILABLE: "The data warehouse is unavailable right now. No answer was produced.",
    C.MODEL_UNAVAILABLE: "The AI model service is unavailable right now. No answer was produced.",
    C.CONTEXT_RETRIEVAL_FAILURE: "The governed context service is unavailable. No answer was produced.",
    C.EVALUATION_REGRESSION: "An evaluation regression was detected.",
    C.UNKNOWN_ERROR: "Something went wrong. The team has been notified.",
}

_MODEL_CODES = ("provider_", "model_timeout", "output_truncated")
_LOOP_CODES = {
    "max_steps_exceeded",
    "token_budget_exceeded",
    "no_final_response",
    "invalid_final_response",
}
_RESOURCE = {
    "execution.timeout",
    "execution.cost_limit_exceeded",
    "sql_rejected.too_complex",
    "sql_rejected.too_long",
    "tool_call_budget_exceeded",
    "execution.result_too_large",
}
_DB_DOWN = {"execution.warehouse_unavailable", "execution.worker_failed", "execution.worker_error"}
_PERMISSION = {"capability_denied", "sql_rejected.relation_not_allowed", "sql_rejected.column_not_allowed"}
_SQLGEN = {
    "sql_rejected.parse_error",
    "sql_rejected.function_not_allowed",
    "sql_rejected.unsupported_syntax",
    "sql_rejected.not_a_query",
    "sql_rejected.multiple_statements",
    "sql_rejected.empty",
}


@dataclass
class FailureRecord:
    failure_id: str
    trace_id: str
    category: FailureCategory
    question: str
    safe_explanation: str
    context_ids: list[str]
    evidence: dict[str, Any]
    retryable: bool
    remediation: str
    context_fix_appropriate: bool
    fix_kind: str | None  # "proposal" | "human_decision" | None
    status: str = "OPEN"
    context_version: str | None = None
    created_at: str = field(default_factory=lambda: dt.datetime.now(dt.UTC).isoformat())

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        return d


def context_ids_from(resp: AgentResponse) -> list[str]:
    ids: list[str] = [*resp.referenced_metrics, *resp.referenced_terms, *resp.referenced_assets]
    for tc in resp.tool_calls:
        s = tc.summary or {}
        for key in ("id", "asset"):
            v = s.get(key)
            if isinstance(v, str):
                ids.append(v)
        for v in s.get("result_ids") or []:
            if isinstance(v, str):
                ids.append(v)
        if tc.tool in ("get_definition", "get_asset") and tc.arguments:
            for v in tc.arguments.values():
                if isinstance(v, str) and "." in v and " " not in v:
                    ids.append(v)
    return list(dict.fromkeys(ids))


def _codes(resp: AgentResponse) -> list[str]:
    codes = [q.error_code for q in resp.queries if q.error_code]
    codes += [t.error_code for t in resp.tool_calls if t.error_code]
    return [c for c in codes if c]


def classify(resp: AgentResponse, store: ContextStore | None = None) -> FailureRecord | None:
    """Return a failure record, or ``None`` for a successful answer without quality findings."""
    err = resp.error.code if resp.error else None
    codes = _codes(resp)
    ids = context_ids_from(resp)
    unsupported = [i for i in ids if store is not None and i in store.unsupported]
    evidence: dict[str, Any] = {
        "outcome": resp.outcome.value,
        "error_code": err,
        "error_stage": resp.error.stage if resp.error else None,
        "query_error_codes": codes,
        "queries": [
            {"query_id": q.query_id, "ok": q.ok, "error_code": q.error_code, "sql": q.generated_sql[:2000]}
            for q in resp.queries
        ],
        "tools_called": [t.tool for t in resp.tool_calls],
        "unsupported_concepts": unsupported,
        "certification_status": resp.certification_status,
        "context_mode": resp.context_mode,
    }
    retryable = bool(resp.error and resp.error.retryable)
    cat: FailureCategory | None = None

    if err and (err.startswith(_MODEL_CODES) or err in {"cancelled"}):
        cat = C.MODEL_UNAVAILABLE if err != "cancelled" else C.UNKNOWN_ERROR
        if err == "model_refusal":
            cat = C.UNKNOWN_ERROR
    elif err == "context_source_unavailable":
        cat = C.CONTEXT_RETRIEVAL_FAILURE
    elif any(c in _DB_DOWN for c in codes):
        cat, retryable = C.DATABASE_UNAVAILABLE, True
    elif resp.outcome == Outcome.POLICY_DENIED or any(c in _PERMISSION for c in codes):
        cat = C.PERMISSION_DENIED
    elif any(c in _RESOURCE for c in codes) or err == "deadline_exceeded":
        cat = C.QUERY_RESOURCE_LIMIT
    elif resp.outcome == Outcome.EXECUTION_ERROR:
        if any(c in _SQLGEN for c in codes) or err == "sql_repair_budget_exhausted":
            cat = C.SQL_GENERATION_FAILURE
        elif any(c.startswith("execution.") for c in codes):
            cat = C.SQL_EXECUTION_FAILURE
        elif err in _LOOP_CODES:
            cat = C.SQL_GENERATION_FAILURE if codes else C.UNKNOWN_ERROR
        else:
            cat = C.UNKNOWN_ERROR
    elif resp.outcome == Outcome.CLARIFICATION_REQUIRED:
        ambiguous = [
            i for i in unsupported if store is not None and store.unsupported[i].classification == "ambiguous"
        ]
        if ambiguous:
            cat = C.AMBIGUOUS_METRIC
            evidence["ambiguous_concepts"] = ambiguous
        else:
            # The question itself was unclear; not a context failure. Recorded for clarification metrics.
            return None
    elif resp.outcome in (Outcome.INSUFFICIENT_CONTEXT, Outcome.UNSUPPORTED_QUESTION):
        hard = [
            i for i in unsupported if store is not None and store.unsupported[i].classification != "ambiguous"
        ]
        if hard:
            cat = C.INSUFFICIENT_SOURCE_DATA
        elif unsupported:
            cat = C.AMBIGUOUS_METRIC
        elif not any(i.startswith(("metric.", "term.")) for i in ids):
            cat = C.MISSING_BUSINESS_DEFINITION
        else:
            cat = (
                C.CONTRADICTORY_CONTEXT
                if len([i for i in ids if i.startswith("metric.")]) > 1
                else C.MISSING_BUSINESS_DEFINITION
            )
    elif resp.outcome == Outcome.ANSWER and resp.certification_status == "deprecated_used":
        cat = C.DEPRECATED_ASSET_USAGE
    if cat is None:
        return None
    remediation, fix_kind = REMEDIATION[cat]
    return FailureRecord(
        failure_id=f"fail-{uuid.uuid4().hex[:12]}",
        trace_id=resp.trace_id,
        category=cat,
        question=resp.question,
        safe_explanation=SAFE_TEXT[cat],
        context_ids=ids,
        evidence=evidence,
        retryable=retryable,
        remediation=remediation,
        context_fix_appropriate=fix_kind is not None,
        fix_kind=fix_kind,
        context_version=resp.context_version,
    )


# Categories a HUMAN answer reviewer may assign when rejecting a wrong answer in shadow mode.
REVIEWER_CATEGORIES = [
    C.MISSING_BUSINESS_DEFINITION,
    C.AMBIGUOUS_METRIC,
    C.MISSING_COLUMN_DESCRIPTION,
    C.MISSING_JOIN_GUIDANCE,
    C.INCORRECT_JOIN_GRAIN,
    C.DEPRECATED_ASSET_USAGE,
    C.CONTRADICTORY_CONTEXT,
    C.INSUFFICIENT_SOURCE_DATA,
    C.SQL_GENERATION_FAILURE,
    C.UNKNOWN_ERROR,
]


def from_reviewer(resp: AgentResponse, category: FailureCategory, reviewer: str, note: str) -> FailureRecord:
    """A human reviewer judged an ANSWER wrong (shadow mode). The diagnosis is the human's."""
    remediation, fix_kind = REMEDIATION[category]
    rec = classify(resp) or FailureRecord(
        failure_id=f"fail-{uuid.uuid4().hex[:12]}",
        trace_id=resp.trace_id,
        category=category,
        question=resp.question,
        safe_explanation=SAFE_TEXT[category],
        context_ids=context_ids_from(resp),
        evidence={"outcome": resp.outcome.value, "executed_sql": resp.executed_sql},
        retryable=False,
        remediation=remediation,
        context_fix_appropriate=fix_kind is not None,
        fix_kind=fix_kind,
        context_version=resp.context_version,
    )
    rec.category = category
    rec.remediation, rec.fix_kind = remediation, fix_kind
    rec.context_fix_appropriate = fix_kind is not None
    rec.safe_explanation = SAFE_TEXT[category]
    rec.evidence["human_diagnosis"] = {"reviewer": reviewer, "note": note[:2000]}
    rec.evidence["executed_sql"] = resp.executed_sql
    return rec


class FailureStore:
    def __init__(self, db: Any, audit: Any) -> None:
        self.db = db
        self.audit = audit

    def record(self, rec: FailureRecord, *, actor: str = "system.classifier") -> FailureRecord:
        with self.db.tx() as c:
            existing = c.execute(
                "SELECT failure_id FROM failures WHERE trace_id = ?", (rec.trace_id,)
            ).fetchone()
            if existing:  # idempotent per trace
                rec.failure_id = existing[0]
                return rec
            c.execute(
                "INSERT INTO failures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.failure_id,
                    rec.trace_id,
                    rec.category.value,
                    rec.question,
                    rec.safe_explanation,
                    json.dumps(rec.context_ids),
                    json.dumps(rec.evidence, default=str),
                    int(rec.retryable),
                    rec.remediation,
                    int(rec.context_fix_appropriate),
                    "OPEN" if rec.context_fix_appropriate else "NOT_ACTIONABLE",
                    rec.context_version,
                    rec.created_at,
                    rec.created_at,
                ),
            )
            self.audit.append(
                action="failure.classified",
                actor_id=actor,
                actor_type="human" if not actor.startswith("system") else "system",
                resource_type="failure",
                resource_id=rec.failure_id,
                new_state=rec.category.value,
                trace_id=rec.trace_id,
                context_version=rec.context_version,
                details={"context_fix_appropriate": rec.context_fix_appropriate, "fix_kind": rec.fix_kind},
                conn=c,
            )
        return rec

    def get(self, failure_id: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM failures WHERE failure_id = ?", (failure_id,))
        if r is None:
            return None
        d = dict(r)
        d["context_ids"] = json.loads(d["context_ids"])
        d["evidence"] = json.loads(d["evidence"])
        return d

    def set_status(self, failure_id: str, status: str) -> None:
        with self.db.tx() as c:
            c.execute(
                "UPDATE failures SET status = ?, updated_at = datetime('now') WHERE failure_id = ?",
                (status, failure_id),
            )
