"""Typed request/response schemas shared by every interface (MCP, CLI, future Slack/eval)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RequestContext(BaseModel):
    """Caller-supplied correlation data. Deliberately has NO identity/role fields: identity comes from
    the server-side trust boundary. Unknown keys (e.g. ``role``) are rejected."""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    purpose: str | None = Field(default=None, max_length=500)


class ErrorInfo(BaseModel):
    code: str
    message: str


class Envelope(BaseModel):
    ok: bool = True
    request_id: str
    context_version: str
    principal: str
    context_mode: str = "full_context"
    error: ErrorInfo | None = None
    warnings: list[str] = Field(default_factory=list)


ResultType = Literal["asset", "column", "glossary_term", "metric", "join", "unsupported_concept"]


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    types: list[ResultType] | None = None
    domain: str | None = Field(default=None, max_length=100)
    layers: list[Literal["raw", "serving"]] | None = None
    include_deprecated: bool = True


class SearchResultItem(BaseModel):
    id: str
    name: str
    type: ResultType
    summary: str
    certification_status: str | None
    deprecated: bool
    source_file: str | None
    context_version: str
    relevance_score: float
    matched_terms: list[str]
    parent_id: str | None = None
    queryable: bool | None = None


class SearchResponse(Envelope):
    query: str = ""
    results: list[SearchResultItem] = Field(default_factory=list)
    missing_context_explanation: str | None = None


class AssetResponse(Envelope):
    asset: dict[str, Any] | None = None
    certification_status: str | None = None
    queryable: bool = False
    queryable_columns: list[str] = Field(default_factory=list)
    withheld_columns: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    source_file: str | None = None


class DefinitionResponse(Envelope):
    resolution: Literal[
        "metric", "glossary_term", "unsupported_concept", "ambiguous", "not_found", "not_visible"
    ] = "not_found"
    id: str | None = None
    certification_status: str | None = None
    definition: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    source_file: str | None = None


class JoinStep(BaseModel):
    join_id: str
    from_asset: str
    to_asset: str
    on: list[dict[str, str]]
    cardinality: str
    causes_fanout: bool
    fanout_warning: str | None
    bridge: str | None
    certification_status: str


class JoinPathResponse(Envelope):
    source_asset: str = ""
    target_asset: str = ""
    found: bool = False
    steps: list[JoinStep] = Field(default_factory=list)
    fanout_risk: bool = False
    sql_join_hint: str | None = None


class PolicyResponse(Envelope):
    target: str = ""
    sensitivity: str | None = None
    queryable_by_caller: bool = False
    reason: str | None = None
    handling: str | None = None
    caller_capabilities: list[str] = Field(default_factory=list)
    caller_max_sensitivity: str | None = None
    caller_max_rows: int | None = None
    asset_certification_status: str | None = None
    deprecated: bool = False
    policy_id: str | None = None
    policy_version: int | None = None


class ExplainResponse(Envelope):
    query_id: str = ""
    valid: bool = False
    executable_sql: str | None = None
    relations: list[str] = Field(default_factory=list)
    columns_read: list[str] = Field(default_factory=list)
    max_sensitivity: str | None = None
    plan: str | None = None
    estimated_max_cardinality: int | None = None
    cost_limit_estimated_rows: int | None = None
    within_cost_limit: bool | None = None
    note: str = (
        "Plan cost is an advisory estimate; hard limits (timeout, rows, memory) are enforced at execution."
    )


class AssetRef(BaseModel):
    asset_id: str
    certification_status: str | None  # None when the context mode does not expose certification
    deprecated: bool


class QueryResponse(Envelope):
    query_id: str = ""
    columns: list[str] = Field(default_factory=list)
    column_types: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    truncation_reason: str | None = None
    max_rows: int | None = None
    executable_sql: str | None = None
    asset_refs: list[AssetRef] = Field(default_factory=list)
    max_sensitivity: str | None = None
    redactions: int = 0
    elapsed_ms: float | None = None


class ValidateContextResponse(Envelope):
    valid: bool = False
    error_count: int = 0
    warning_count: int = 0
    issues: list[str] = Field(default_factory=list)
