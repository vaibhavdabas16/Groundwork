"""Structured agent request/response objects.

``AgentResponse`` is the single contract consumed by the CLI, the evaluation harness and (Phase 3)
the Slack integration. Every field is populated by deterministic runtime code from recorded tool
results; the model only contributes the natural-language answer, assumptions and the outcome it
proposes (which the runtime validates).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Outcome(StrEnum):
    ANSWER = "ANSWER"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"
    UNSUPPORTED_QUESTION = "UNSUPPORTED_QUESTION"
    POLICY_DENIED = "POLICY_DENIED"
    EXECUTION_ERROR = "EXECUTION_ERROR"


ABSTENTIONS = frozenset(
    {
        Outcome.CLARIFICATION_REQUIRED,
        Outcome.INSUFFICIENT_CONTEXT,
        Outcome.UNSUPPORTED_QUESTION,
        Outcome.POLICY_DENIED,
    }
)


class AgentConfig(BaseModel):
    """Everything that must be held constant across experimental conditions."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    temperature: float | None = None  # rejected by providers/API versions that removed sampling params
    max_output_tokens_per_turn: int = Field(default=8000, ge=256, le=64000)
    request_timeout_seconds: float = Field(default=180.0, gt=0)
    max_provider_retries: int = Field(default=3, ge=0, le=8)
    max_steps: int = Field(default=14, ge=1, le=50)
    max_sql_repairs: int = Field(default=3, ge=0, le=10)
    max_total_tokens: int = Field(default=600_000, ge=1_000)
    deadline_seconds: float = Field(default=420.0, gt=0)
    mcp_call_timeout_seconds: float = Field(default=60.0, gt=0)
    max_tool_result_chars: int = Field(default=16_000, ge=1_000)
    result_rows_to_model: int = Field(default=60, ge=1, le=1000)
    prompt_caching: bool = True  # caches the input prefix only; responses are never reused


class ToolCallRecord(BaseModel):
    step: int
    tool: str
    arguments: dict[str, Any] | None
    ok: bool
    error_code: str | None = None
    invalid: bool = False  # unknown tool / schema-invalid / malformed arguments
    duration_ms: float = 0.0
    summary: dict[str, Any] = Field(default_factory=dict)  # never raw result rows


class QueryRecord(BaseModel):
    query_id: str
    generated_sql: str
    executed_sql: str | None
    ok: bool
    error_code: str | None = None
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float | None = None
    columns: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)


class UsageInfo(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    model_calls: int = 0
    provider_retries: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def add(self, other: UsageInfo) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.model_calls += other.model_calls
        self.provider_retries += other.provider_retries


class ModelInfo(BaseModel):
    provider: str
    requested_model: str
    resolved_models: list[str] = Field(default_factory=list)  # as reported by the provider, per call
    live: bool  # False only for test doubles (never reported as a model evaluation)
    parameters: dict[str, Any] = Field(default_factory=dict)
    provider_request_ids: list[str] = Field(default_factory=list)


class CostInfo(BaseModel):
    estimated_usd: float | None
    pricing_version: str
    basis: str = "provider-reported token usage x documented list prices"


class ErrorDetail(BaseModel):
    code: str
    message: str
    stage: str
    retryable: bool = False


class AgentResponse(BaseModel):
    request_id: str
    outcome: Outcome
    question: str
    answer: str
    clarification_question: str | None = None
    answer_basis: Literal["query_result", "metadata", "none"] = "none"
    generated_sql: str | None = None
    executed_sql: str | None = None
    query_id: str | None = None
    referenced_assets: list[str] = Field(default_factory=list)
    referenced_metrics: list[str] = Field(default_factory=list)
    referenced_terms: list[str] = Field(default_factory=list)
    context_version: str | None = None
    context_mode: str
    certification_status: Literal["verified", "draft", "deprecated_used", "mixed", "not_exposed", "none"] = (
        "none"
    )
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    result_columns: list[str] = Field(default_factory=list)
    result_preview: list[list[Any]] = Field(default_factory=list)
    result_row_count: int | None = None
    result_truncated: bool = False
    queries: list[QueryRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    model: ModelInfo
    usage: UsageInfo = Field(default_factory=UsageInfo)
    cost: CostInfo
    latency_ms: float
    steps: int = 0
    sql_repair_attempts: int = 0
    invalid_tool_calls: int = 0
    error: ErrorDetail | None = None
    prompt_version: str
    prompt_hash: str
    trace_id: str
