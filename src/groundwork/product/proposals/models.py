"""Context-fix proposal models and the explicit lifecycle state machine."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class State(StrEnum):
    DETECTED = "DETECTED"
    DIAGNOSED = "DIAGNOSED"
    PROPOSED = "PROPOSED"
    VALIDATING = "VALIDATING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    MERGE_ELIGIBLE = "MERGE_ELIGIBLE"
    MERGED = "MERGED"
    DEPLOYED = "DEPLOYED"
    BLOCKED = "BLOCKED"
    SUPERSEDED = "SUPERSEDED"
    VALIDATION_FAILED = "VALIDATION_FAILED"


S = State
TERMINAL = frozenset({S.REJECTED, S.DEPLOYED, S.SUPERSEDED})

# Allowed transitions. Anything else raises. Note there is NO edge into APPROVED except from
# AWAITING_REVIEW, and APPROVED requires a human decision recorded by ``ProposalStore.decide``.
TRANSITIONS: dict[State, frozenset[State]] = {
    S.DETECTED: frozenset({S.DIAGNOSED, S.BLOCKED, S.SUPERSEDED}),
    S.DIAGNOSED: frozenset({S.PROPOSED, S.BLOCKED, S.SUPERSEDED}),
    S.PROPOSED: frozenset({S.VALIDATING, S.BLOCKED, S.SUPERSEDED}),
    S.VALIDATING: frozenset({S.AWAITING_REVIEW, S.VALIDATION_FAILED, S.BLOCKED}),
    S.VALIDATION_FAILED: frozenset({S.PROPOSED, S.REJECTED, S.SUPERSEDED}),  # PROPOSED = human revision
    S.AWAITING_REVIEW: frozenset(
        {S.APPROVED, S.REJECTED, S.CHANGES_REQUESTED, S.SUPERSEDED, S.PROPOSED, S.BLOCKED}
    ),
    S.CHANGES_REQUESTED: frozenset({S.PROPOSED, S.REJECTED, S.SUPERSEDED, S.BLOCKED}),
    S.APPROVED: frozenset({S.MERGE_ELIGIBLE, S.BLOCKED, S.PROPOSED, S.REJECTED}),
    S.MERGE_ELIGIBLE: frozenset({S.MERGED, S.BLOCKED, S.PROPOSED, S.REJECTED}),
    S.MERGED: frozenset({S.DEPLOYED, S.BLOCKED}),
    S.BLOCKED: frozenset({S.PROPOSED, S.VALIDATING, S.MERGE_ELIGIBLE, S.MERGED, S.REJECTED, S.SUPERSEDED}),
    S.REJECTED: frozenset(),
    S.DEPLOYED: frozenset(),
    S.SUPERSEDED: frozenset(),
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------- structured patch operations (the ONLY way a proposal changes context) ----------


class SetColumnDescription(Strict):
    op: Literal["set_column_description"] = "set_column_description"
    asset_id: str
    column: str
    description: str = Field(min_length=10, max_length=1200)


class AddTermSynonym(Strict):
    op: Literal["add_term_synonym"] = "add_term_synonym"
    target_id: str  # glossary term, metric or unsupported concept
    synonym: str = Field(min_length=2, max_length=80)


class AppendTermNote(Strict):
    op: Literal["append_term_note"] = "append_term_note"
    term_id: str
    field: Literal["ambiguities", "exclusions"]
    text: str = Field(min_length=10, max_length=600)


class AddGlossaryTerm(Strict):
    op: Literal["add_glossary_term"] = "add_glossary_term"
    file: str = "context/glossary/terms.yaml"
    id: str = Field(pattern=r"^term\.[a-z0-9_]+$")
    name: str
    synonyms: list[str] = Field(default_factory=list)
    definition: str = Field(min_length=20, max_length=1200)
    business_interpretation: str | None = None
    domain: str
    owner: str
    assets: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(min_length=1)


class AddJoin(Strict):
    op: Literal["add_join"] = "add_join"
    id: str = Field(pattern=r"^join\.[a-z0-9_]+$")
    left: str
    right: str
    join_columns: list[dict[str, str]]
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many", "zero_or_one_to_one"]
    grain_note: str
    fanout_warning: str | None = None
    observed: str  # REQUIRED: measured evidence (e.g. from a governed query)
    owner: str


class SetFanoutWarning(Strict):
    op: Literal["set_fanout_warning"] = "set_fanout_warning"
    join_id: str
    fanout_warning: str = Field(min_length=20, max_length=800)


class AddMetricCaveat(Strict):
    op: Literal["add_metric_caveat"] = "add_metric_caveat"
    metric_id: str
    field: Literal["known_issues", "assumptions"] = "known_issues"
    text: str = Field(min_length=10, max_length=600)


class ChangeMetricSql(Strict):  # ELEVATED review
    op: Literal["change_metric_sql"] = "change_metric_sql"
    metric_id: str
    sql: str = Field(min_length=10, max_length=4000)
    double_counting_check: str


class DeprecateObject(Strict):  # ELEVATED review
    op: Literal["deprecate"] = "deprecate"
    target_id: str  # asset or metric
    reason: str = Field(min_length=10)
    replaced_by: str | None = None


class AddUnsupportedConcept(Strict):
    op: Literal["add_unsupported"] = "add_unsupported"
    id: str = Field(pattern=r"^unsupported\.[a-z0-9_]+$")
    name: str
    synonyms: list[str] = Field(default_factory=list)
    classification: Literal["unsupported", "ambiguous"]
    reason: str = Field(min_length=20)
    candidate_definitions: list[str] = Field(default_factory=list)
    decision_needed_from: str
    guidance: str = Field(min_length=10)
    source_data_required: str | None = None  # appended to `reason` (missing source requirement)


PatchOp = Annotated[
    SetColumnDescription
    | AddTermSynonym
    | AppendTermNote
    | AddGlossaryTerm
    | AddJoin
    | SetFanoutWarning
    | AddMetricCaveat
    | ChangeMetricSql
    | DeprecateObject
    | AddUnsupportedConcept,
    Field(discriminator="op"),
]

ELEVATED_OPS = frozenset({"change_metric_sql", "deprecate"})


class HumanDecisionRequest(Strict):
    question: str = Field(min_length=10)
    options: list[str] = Field(default_factory=list)
    decision_owner: str  # owner id from owners.yaml


class Evidence(Strict):
    kind: Literal["schema", "context", "trace", "data_quality", "governed_query", "eval_case"]
    ref: str  # e.g. "asset.analytics.customers:customer_unique_key", trace id, query id
    detail: str


class ProposalDraft(Strict):
    """What the improvement engine (LLM or deterministic) submits. Validated before storing."""

    kind: Literal["context_change", "human_decision", "no_action"]
    summary: str = Field(min_length=10, max_length=600)
    deficiency: str = Field(min_length=10)
    operations: list[PatchOp] = Field(default_factory=list)
    decision_request: HumanDecisionRequest | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    no_action_reason: str | None = None


class FileDiff(Strict):
    path: str
    before_sha256: str | None
    after_sha256: str
    unified_diff: str


class ContextProposal(Strict):
    proposal_id: str
    revision: int
    category: str
    title: str
    draft: ProposalDraft
    affected_ids: list[str]
    domains: list[str]
    review_tier: Literal["standard", "elevated"]
    diffs: list[FileDiff]
    base_context_version: str
    proposed_context_version: str | None
    author: str
    author_type: Literal["agent", "human", "system"]
    trace_ids: list[str]
    failure_ids: list[str]
    eval_case_ids: list[str]
    created_at: str
    revision_note: str | None = None

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
