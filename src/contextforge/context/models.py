"""Typed schemas for the governed context layer.

Every context document is validated by these Pydantic models (``extra="forbid"`` so typos are
rejected instead of silently ignored). Cross-document rules (references, certification, cycles,
unsafe SQL) live in ``validation.py``.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

StableId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$", max_length=128)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Status(StrEnum):
    draft = "draft"
    verified = "verified"
    deprecated = "deprecated"


class Sensitivity(StrEnum):
    """Ordered from least to most sensitive."""

    public = "public"
    internal = "internal"
    pseudonymous = "pseudonymous"
    restricted = "restricted"
    pii = "pii"

    @property
    def rank(self) -> int:
        return list(Sensitivity).index(self)


class Certification(Strict):
    status: Status
    certified_by: StableId | None = None
    certified_at: datetime | None = None
    decision_ref: str | None = None
    notes: str | None = None
    deprecated_reason: str | None = None
    replaced_by: StableId | None = None


class Provenance(Strict):
    authored_by: StableId
    authored_at: date
    generated_by: Literal["human", "ai", "human_and_ai"]
    evidence: list[str] = Field(default_factory=list)


class QualityWarning(Strict):
    id: str
    description: str
    check_ref: str | None = None  # id of a check in warehouse/quality.py


class Column(Strict):
    name: str
    data_type: str
    description: str
    sensitivity: Sensitivity = Sensitivity.internal
    nullable: bool = True
    notes: str | None = None


class ForeignKey(Strict):
    columns: list[str]
    references: StableId  # asset id
    ref_columns: list[str]
    enforced_in_source: bool = False
    observed_violations: str | None = None


class Lineage(Strict):
    sources: list[StableId] = Field(default_factory=list)
    transformation: str | None = None


class Asset(Strict):
    id: StableId
    name: str
    kind: Literal["table", "view"] = "table"
    layer: Literal["raw", "serving"]
    database: str
    schema_name: str = Field(alias="schema")
    table: str
    description: str
    grain: str
    domain: str
    owner: StableId
    steward: StableId | None = None
    sensitivity: Sensitivity
    columns: list[Column]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    certification: Certification
    provenance: Provenance
    quality_warnings: list[QualityWarning] = Field(default_factory=list)
    lineage: Lineage = Field(default_factory=Lineage)
    last_reviewed_at: date | None = None
    metrics: list[StableId] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.table}"

    @property
    def deprecated(self) -> bool:
        return self.certification.status == Status.deprecated

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


class GlossaryTerm(Strict):
    id: StableId
    name: str
    synonyms: list[str] = Field(default_factory=list)
    definition: str
    business_interpretation: str | None = None
    domain: str
    owner: StableId
    assets: list[StableId] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)  # "asset_id:column"
    metrics: list[StableId] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    certification: Certification
    provenance: Provenance


class MetricPart(Strict):
    description: str
    expression: str


class Dimension(Strict):
    name: str
    expression: str
    asset: StableId
    join_path_note: str | None = None


class TimestampSemantics(Strict):
    column: str
    meaning: str
    timezone: str
    timezone_basis: str


class Metric(Strict):
    id: StableId
    name: str
    synonyms: list[str] = Field(default_factory=list)
    definition: str
    business_purpose: str
    sql: str
    sql_status: Literal["draft", "approved"] = "draft"
    numerator: MetricPart | None = None
    denominator: MetricPart | None = None
    aggregation_grain: str
    required_filters: list[str] = Field(default_factory=list)
    timestamp: TimestampSemantics
    inclusions: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    supported_dimensions: list[Dimension] = Field(default_factory=list)
    depends_on: list[StableId] = Field(default_factory=list)
    assets: list[StableId]
    owner: StableId
    certification: Certification
    version: int = Field(ge=1)
    provenance: Provenance
    assumptions: list[str] = Field(default_factory=list)
    double_counting_check: str
    known_issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ratio_parts(self) -> Metric:
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("numerator and denominator must be given together")
        return self


class JoinColumnPair(Strict):
    left: str
    right: str


class JoinRelationship(Strict):
    id: StableId
    left: StableId
    right: StableId
    join_columns: list[JoinColumnPair]
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many", "zero_or_one_to_one"]
    direction: Literal["left_to_right", "right_to_left", "both"] = "both"
    grain_note: str
    fanout_warning: str | None = None
    metric_restrictions: list[str] = Field(default_factory=list)
    bridge: str | None = None
    observed: str | None = None
    owner: StableId
    certification: Certification
    provenance: Provenance


class UnsupportedConcept(Strict):
    id: StableId
    name: str
    synonyms: list[str] = Field(default_factory=list)
    classification: Literal["unsupported", "ambiguous"]
    reason: str
    candidate_definitions: list[StableId] = Field(default_factory=list)
    decision_needed_from: StableId
    guidance: str


class Owner(Strict):
    id: StableId
    kind: Literal["human", "team", "agent", "service"]
    name: str
    placeholder: bool = False
    can_certify: bool = False
    notes: str | None = None


# ---------------- governance policy ----------------


class Capability(StrEnum):
    metadata_read = "metadata.read"
    metrics_read_verified = "metrics.read_verified"
    metrics_read_draft = "metrics.read_draft"
    query_explain = "query.explain"
    query_execute = "query.execute"
    context_admin = "context.admin"


class SensitivityClass(Strict):
    sensitivity: Sensitivity
    description: str
    agent_queryable: bool
    handling: str


class Role(Strict):
    id: StableId
    description: str
    capabilities: list[Capability]
    queryable_assets: list[StableId] = Field(default_factory=list)  # explicit allow-list
    max_sensitivity: Sensitivity
    allowed_certification: list[Status]
    include_deprecated: bool = False
    max_rows: int = Field(ge=1)


class Principal(Strict):
    id: StableId
    kind: Literal["human", "service", "agent"]
    roles: list[StableId]
    description: str


class QueryPolicy(Strict):
    allowed_statement_types: list[Literal["select"]]
    default_max_rows: int = Field(ge=1)
    max_cost_estimated_rows: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    max_concurrent_queries: int = Field(ge=1)
    max_tool_calls_per_request: int = Field(ge=1)
    allowed_functions: list[str]


class RetentionPolicy(Strict):
    query_audit_log_days: int = Field(ge=1)
    store_result_rows: bool
    store_sql_text: bool
    notes: str


class Policy(Strict):
    id: StableId
    version: int = Field(ge=1)
    sensitivity_classes: list[SensitivityClass]
    roles: list[Role]
    principals: list[Principal]
    tool_permissions: dict[str, Capability]
    query: QueryPolicy
    retention: RetentionPolicy
    certification_rules: list[str]
    owner: StableId
    provenance: Provenance
