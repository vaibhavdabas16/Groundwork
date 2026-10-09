"""Constrained, observable agent runtime.

Deterministic code owns: the step loop and its limits (steps, SQL repairs, tokens, deadline),
tool permission (only the MCP tools in ``AGENT_TOOLS`` plus ``submit_response``), request
correlation (the trace id is injected as ``request_context.request_id`` on every MCP call - the
model cannot choose it), validation of the model's final response, and every provenance field of
``AgentResponse`` (SQL, executed SQL, result, referenced assets, certification). The LLM decides
which tools to call, writes SQL, and writes the natural-language answer.

The only route to data is ``McpToolClient`` -> Phase 1 MCP server -> ContextForgeService ->
SQL guard -> killable read-only worker. There is no other database connection in this package.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contextforge.agent.mcp_client import AGENT_TOOLS, McpToolClient, McpUnavailable
from contextforge.agent.pricing import estimate_cost_usd, pricing_version
from contextforge.agent.prompts import (
    PROMPT_VERSION,
    SUBMIT_TOOL_DESCRIPTION,
    SUBMIT_TOOL_NAME,
    SUBMIT_TOOL_SCHEMA,
    SYSTEM_PROMPT,
    prompt_hash,
    user_message,
)
from contextforge.agent.providers.base import (
    ModelProvider,
    ModelTurn,
    ProviderError,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolSpec,
)
from contextforge.agent.schemas import (
    AgentConfig,
    AgentResponse,
    CostInfo,
    ErrorDetail,
    ModelInfo,
    Outcome,
    QueryRecord,
    ToolCallRecord,
    UsageInfo,
)
from contextforge.agent.traces import TraceStore
from contextforge.config.settings import ContextMode
from contextforge.observability.logging import get_logger

log = get_logger("agent")

MAX_QUESTION_CHARS = 2000
POLICY_ERROR_CODES = frozenset(
    {
        "capability_denied",
        "tool_not_permitted",
        "not_visible",
        "sql_rejected.column_not_allowed",
        "sql_rejected.relation_not_allowed",
        "sql_rejected.sensitive_column",
    }
)


class SubmitArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    answer: str = Field(min_length=1, max_length=8000)
    query_id: str | None = None
    clarification_question: str | None = Field(default=None, max_length=2000)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    warnings: list[str] = Field(default_factory=list, max_length=30)
    referenced_metrics: list[str] = Field(default_factory=list, max_length=50)
    referenced_terms: list[str] = Field(default_factory=list, max_length=50)
    referenced_assets: list[str] = Field(default_factory=list, max_length=50)


class _Abort(Exception):
    def __init__(self, code: str, message: str, stage: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.detail = ErrorDetail(code=code, message=message, stage=stage, retryable=retryable)


@dataclass
class _QueryState:
    record: QueryRecord
    rows: list[list[Any]] = field(default_factory=list)


@dataclass
class AgentResult:
    """The response plus the in-memory full result of the supporting query (never persisted)."""

    response: AgentResponse
    result_columns: list[str]
    result_rows: list[list[Any]]
    result_sha256: str | None


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local ``$ref``s (``#/$defs/...``) so every provider sees a self-contained schema."""
    defs = schema.get("$defs", {})

    def walk(node: Any, depth: int = 0) -> Any:
        if depth > 20:
            return node
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str) and node["$ref"].startswith("#/$defs/"):
                target = defs.get(node["$ref"].split("/")[-1], {})
                merged = {
                    **walk(copy.deepcopy(target), depth + 1),
                    **{k: v for k, v in node.items() if k != "$ref"},
                }
                return merged
            return {k: walk(v, depth + 1) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v, depth + 1) for v in node]
        return node

    out: dict[str, Any] = walk(schema)
    return out


def agent_tool_specs(mcp: McpToolClient) -> list[ToolSpec]:
    """MCP tool schemas as exposed to the model: identical in every context mode.

    ``request_context`` is removed - the runtime injects it so the model cannot alter correlation
    or budgets; ``validate_context`` (admin) is never exposed.
    """
    specs = []
    for name in AGENT_TOOLS:
        t = mcp.tools[name]
        schema = _inline_refs(t.input_schema)
        props = dict(schema.get("properties", {}))
        props.pop("request_context", None)
        schema["properties"] = props
        schema["required"] = [r for r in schema.get("required", []) if r != "request_context"]
        schema.pop("title", None)
        specs.append(ToolSpec(name, t.description, schema))
    specs.append(ToolSpec(SUBMIT_TOOL_NAME, SUBMIT_TOOL_DESCRIPTION, SUBMIT_TOOL_SCHEMA))
    return specs


def canonical_result_hash(columns: list[str], rows: list[list[Any]]) -> str:
    payload = json.dumps({"c": columns, "r": rows}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class DataAgent:
    def __init__(
        self,
        provider: ModelProvider,
        mcp: McpToolClient,
        config: AgentConfig,
        *,
        mode: ContextMode,
        trace_store: TraceStore | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        self.provider = provider
        self.mcp = mcp
        self.config = config
        self.mode = mode
        self.traces = trace_store
        self.provenance = provenance or {}
        self.tool_specs = agent_tool_specs(mcp)
        self.prompt_hash = prompt_hash(
            [
                {"name": s.name, "description": s.description, "input_schema": s.input_schema}
                for s in self.tool_specs
            ]
        )

    # ------------------------------------------------------------------ public API
    async def ask(
        self,
        question: str,
        *,
        request_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
        trace_meta: dict[str, Any] | None = None,
    ) -> AgentResult:
        run = _Run(self, question, request_id or f"tr-{uuid.uuid4().hex}", cancel_event)
        result = await run.execute()
        if self.traces is not None:
            meta = trace_meta or {}
            try:
                self.traces.record(
                    result.response,
                    provenance=self.provenance,
                    agent_config=self.config.model_dump(mode="json"),
                    result_sha256=result.result_sha256,
                    eval_run_id=meta.get("eval_run_id"),
                    case_id=meta.get("case_id"),
                    attempt_id=meta.get("attempt_id"),
                    replay_of=meta.get("replay_of"),
                )
            except Exception as exc:  # tracing must not change the outcome, but must be visible
                log.error(
                    "trace write failed", extra={"trace_id": result.response.trace_id, "error": str(exc)}
                )
                result.response.warnings.append("trace persistence failed")
        return result


class _Run:
    """State of one request. Not reusable."""

    def __init__(
        self, agent: DataAgent, question: str, trace_id: str, cancel_event: asyncio.Event | None
    ) -> None:
        self.a = agent
        self.cfg = agent.config
        self.question = question
        self.trace_id = trace_id
        self.cancel_event = cancel_event
        self.t0 = time.perf_counter()
        self.deadline = time.monotonic() + self.cfg.deadline_seconds
        self.usage = UsageInfo()
        self.model_info = ModelInfo(
            provider=agent.provider.name,
            requested_model=self.cfg.model if agent.provider.live else agent.provider.name,
            live=agent.provider.live,
            parameters=agent.provider.parameters(),
        )
        self.tool_calls: list[ToolCallRecord] = []
        self.queries: dict[str, _QueryState] = {}
        self.query_order: list[str] = []
        self.seen_ids: set[str] = set()
        self.cert_by_id: dict[str, str] = {}
        self.context_version: str | None = None
        self.failed_queries = 0
        self.invalid_calls = 0
        self.policy_denials = 0
        self.steps = 0
        self.warnings: list[str] = []

    # ---------------------------------------------------------------- helpers
    def _remaining(self) -> float:
        return self.deadline - time.monotonic()

    async def _bounded(self, coro: Any, timeout: float, stage: str) -> Any:
        """Await ``coro`` with a timeout and cooperative cancellation."""
        if timeout <= 0:
            raise _Abort("deadline_exceeded", "request deadline exceeded", stage)
        task = asyncio.ensure_future(coro)
        waiters: set[asyncio.Future[Any]] = {task}
        cancel_task: asyncio.Future[Any] | None = None
        if self.cancel_event is not None:
            cancel_task = asyncio.ensure_future(self.cancel_event.wait())
            waiters.add(cancel_task)
        try:
            done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
        if task in done:
            return task.result()
        task.cancel()
        with contextlib.suppress(BaseException):  # we only need the task to finish cancelling
            await task
        if cancel_task is not None and cancel_task in done:
            raise _Abort("cancelled", "request cancelled by caller", stage)
        code = (
            "model_timeout" if stage == "model" else "mcp_timeout" if stage == "tool" else "deadline_exceeded"
        )
        raise _Abort(code, f"{stage} step exceeded its time limit ({timeout:.1f}s)", stage, retryable=True)

    def _account(self, turn: ModelTurn) -> None:
        self.usage.input_tokens += turn.usage.input_tokens
        self.usage.output_tokens += turn.usage.output_tokens
        self.usage.cache_creation_input_tokens += turn.usage.cache_creation_input_tokens
        self.usage.cache_read_input_tokens += turn.usage.cache_read_input_tokens
        self.usage.model_calls += 1
        self.usage.provider_retries += turn.retries
        if turn.resolved_model:
            self.model_info.resolved_models.append(turn.resolved_model)
            if self.a.provider.live and not turn.resolved_model.startswith(self.cfg.model):
                self.warnings.append(
                    f"provider reported model {turn.resolved_model!r} for requested {self.cfg.model!r}"
                )
        if turn.provider_request_id:
            self.model_info.provider_request_ids.append(turn.provider_request_id)

    def _note_ids(self, tool: str, data: dict[str, Any]) -> None:
        self.context_version = data.get("context_version") or self.context_version
        if tool == "search_assets":
            for r in data.get("results", []):
                self.seen_ids.add(r["id"])
                if r.get("certification_status"):
                    self.cert_by_id[r["id"]] = r["certification_status"]
        elif tool == "get_asset" and data.get("asset"):
            aid = data["asset"].get("id")
            if aid:
                self.seen_ids.add(aid)
                if data.get("certification_status"):
                    self.cert_by_id[aid] = data["certification_status"]
        elif tool == "get_definition":
            if data.get("id"):
                self.seen_ids.add(data["id"])
                if data.get("certification_status"):
                    self.cert_by_id[data["id"]] = data["certification_status"]
            for c in data.get("candidates", []):
                if c.get("id"):
                    self.seen_ids.add(c["id"])
        elif tool == "get_join_path":
            for s in data.get("steps", []):
                self.seen_ids.update([s["join_id"], s["from_asset"], s["to_asset"]])
                self.cert_by_id[s["join_id"]] = s["certification_status"]
        elif tool == "run_query":
            for ref in data.get("asset_refs", []):
                self.seen_ids.add(ref["asset_id"])
                if ref.get("certification_status"):
                    self.cert_by_id[ref["asset_id"]] = ref["certification_status"]

    def _model_view(self, tool: str, data: dict[str, Any]) -> dict[str, Any]:
        """What the model sees of a tool result (rows capped; full rows stay in memory)."""
        if tool == "run_query" and data.get("ok"):
            n = self.cfg.result_rows_to_model
            view = {k: v for k, v in data.items() if k not in ("rows", "principal")}
            view["rows"] = data.get("rows", [])[:n]
            if len(data.get("rows", [])) > n:
                view["rows_shown_to_model"] = n
            return view
        return {k: v for k, v in data.items() if k != "principal"}

    def _encode(self, payload: dict[str, Any]) -> str:
        text = json.dumps({"untrusted_tool_output": payload}, default=str, ensure_ascii=False)
        lim = self.cfg.max_tool_result_chars
        if len(text) > lim:
            text = text[:lim] + "... [TRUNCATED by runtime: output exceeded size limit; narrow your request]"
        return text

    # ---------------------------------------------------------------- tool dispatch
    async def _dispatch(self, call: ToolCall) -> ToolResultPart:
        rec = ToolCallRecord(step=self.steps, tool=call.name, arguments=call.arguments, ok=False)
        self.tool_calls.append(rec)
        if call.name not in AGENT_TOOLS:
            rec.invalid, rec.error_code = True, "unknown_tool"
            self.invalid_calls += 1
            return ToolResultPart(
                call.id, self._encode({"error": "unknown_tool", "allowed": list(AGENT_TOOLS)}), True
            )
        if call.arguments is None:
            rec.invalid, rec.error_code = True, "malformed_arguments"
            self.invalid_calls += 1
            return ToolResultPart(
                call.id, self._encode({"error": "malformed_arguments", "detail": call.raw_error}), True
            )
        args = {k: v for k, v in call.arguments.items() if k != "request_context"}
        args["request_context"] = {"request_id": self.trace_id}
        if call.name == "run_query" and self.failed_queries > self.cfg.max_sql_repairs:
            rec.error_code = "sql_repair_budget_exhausted"
            return ToolResultPart(
                call.id,
                self._encode(
                    {
                        "error": "sql_repair_budget_exhausted",
                        "message": "No more query attempts are allowed. Call submit_response now.",
                    }
                ),
                True,
            )
        res = await self._bounded(
            self.a.mcp.call(
                call.name, args, timeout=min(self.cfg.mcp_call_timeout_seconds, self._remaining())
            ),
            min(self.cfg.mcp_call_timeout_seconds + 5, self._remaining()),
            "tool",
        )
        rec.duration_ms = res.duration_ms
        rec.ok = res.ok
        rec.error_code = res.error_code
        if res.protocol_error:
            if res.error_code == "mcp_transport_error":
                raise _Abort(
                    "context_source_unavailable", res.error_message or "MCP transport failed", "tool"
                )
            if res.error_code == "mcp_timeout":
                rec.error_code = "mcp_timeout"
                return ToolResultPart(call.id, self._encode({"error": "tool_timeout"}), True)
            rec.invalid = True
            self.invalid_calls += 1
            return ToolResultPart(
                call.id, self._encode({"error": "invalid_arguments", "detail": res.error_message}), True
            )
        data = res.data
        if res.error_code in POLICY_ERROR_CODES:
            self.policy_denials += 1
        self._note_ids(call.name, data)
        if call.name == "run_query":
            sql = str(call.arguments.get("sql", ""))
            qid = data.get("query_id") or f"failed-{len(self.query_order) + 1}"
            qr = QueryRecord(
                query_id=qid,
                generated_sql=sql,
                executed_sql=data.get("executable_sql"),
                ok=res.ok,
                error_code=res.error_code,
                row_count=int(data.get("row_count") or 0),
                truncated=bool(data.get("truncated")),
                elapsed_ms=data.get("elapsed_ms"),
                columns=list(data.get("columns") or []),
                asset_ids=[r["asset_id"] for r in data.get("asset_refs", [])],
            )
            self.queries[qid] = _QueryState(qr, list(data.get("rows") or []) if res.ok else [])
            self.query_order.append(qid)
            if not res.ok:
                self.failed_queries += 1
            rec.summary = {"query_id": qid, "row_count": qr.row_count, "columns": qr.columns}
        elif call.name == "search_assets":
            rec.summary = {"result_ids": [r["id"] for r in data.get("results", [])][:20]}
        elif call.name == "get_definition":
            rec.summary = {"resolution": data.get("resolution"), "id": data.get("id")}
        elif call.name == "get_asset":
            rec.summary = {"asset": (data.get("asset") or {}).get("id")}
        return ToolResultPart(call.id, self._encode(self._model_view(call.name, data)), not res.ok)

    # ---------------------------------------------------------------- finalisation
    def _validate_submit(self, call: ToolCall) -> tuple[SubmitArgs | None, str | None]:
        if call.arguments is None:
            return None, "submit_response arguments were not a JSON object"
        try:
            sub = SubmitArgs.model_validate(call.arguments)
        except ValidationError as exc:
            return None, "invalid submit_response: " + "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
            )
        if sub.outcome == Outcome.ANSWER and sub.query_id is not None:
            q = self.queries.get(sub.query_id)
            if q is None or not q.record.ok:
                return None, (
                    f"query_id {sub.query_id!r} is not a successful run_query result in this conversation. "
                    f"Successful query ids: {[k for k in self.query_order if self.queries[k].record.ok]}"
                )
        if sub.outcome == Outcome.CLARIFICATION_REQUIRED and not (sub.clarification_question or "").strip():
            return None, "CLARIFICATION_REQUIRED needs clarification_question"
        return sub, None

    def _certification(self, ids: list[str]) -> str:
        if self.a.mode != ContextMode.full_context:
            return "not_exposed"
        statuses = {self.cert_by_id[i] for i in ids if i in self.cert_by_id}
        if not statuses:
            return "none"
        if "deprecated" in statuses:
            return "deprecated_used"
        if statuses == {"verified"}:
            return "verified"
        if statuses == {"draft"}:
            return "draft"
        return "mixed"

    def _build(
        self,
        outcome: Outcome,
        answer: str,
        *,
        sub: SubmitArgs | None = None,
        error: ErrorDetail | None = None,
    ) -> AgentResult:
        warnings = list(self.warnings)
        metrics: list[str] = []
        terms: list[str] = []
        assets: list[str] = []
        qstate: _QueryState | None = None
        basis = "none"
        if sub is not None:
            warnings.extend(sub.warnings)
            for lst, out in (
                (sub.referenced_metrics, metrics),
                (sub.referenced_terms, terms),
                (sub.referenced_assets, assets),
            ):
                for rid in lst:
                    if rid in self.seen_ids:
                        out.append(rid)
                    else:
                        warnings.append(f"dropped reference not returned by any tool: {rid!r}")
            if sub.outcome == Outcome.ANSWER:
                if sub.query_id:
                    qstate = self.queries[sub.query_id]
                    basis = "query_result"
                else:
                    basis = "metadata"
                    if any(self.queries[q].record.ok for q in self.query_order):
                        warnings.append("ANSWER was not linked to a query result (query_id missing).")
        if qstate is not None:
            for aid in qstate.record.asset_ids:
                if aid not in assets:
                    assets.append(aid)
        cert = self._certification([*metrics, *terms, *assets])
        if cert in ("draft", "mixed") and "draft" not in answer.lower():
            warnings.append("Uses DRAFT context (not human-verified).")
        if cert == "deprecated_used":
            warnings.append("A DEPRECATED definition or asset was referenced.")
        usage = self.usage
        model_for_cost = self.cfg.model if self.a.provider.live else ""
        cols = qstate.record.columns if qstate else []
        rows = qstate.rows if qstate else []
        resp = AgentResponse(
            request_id=self.trace_id,
            trace_id=self.trace_id,
            outcome=outcome,
            question=self.question,
            answer=answer,
            clarification_question=sub.clarification_question if sub else None,
            answer_basis=basis,  # type: ignore[arg-type]
            generated_sql=qstate.record.generated_sql if qstate else None,
            executed_sql=qstate.record.executed_sql if qstate else None,
            query_id=qstate.record.query_id if qstate else None,
            referenced_assets=assets,
            referenced_metrics=metrics,
            referenced_terms=terms,
            context_version=self.context_version,
            context_mode=self.a.mode.value,
            certification_status=cert,  # type: ignore[arg-type]
            assumptions=list(sub.assumptions) if sub else [],
            warnings=list(dict.fromkeys(warnings)),
            result_columns=cols,
            result_preview=rows[:10],
            result_row_count=qstate.record.row_count if qstate else None,
            result_truncated=qstate.record.truncated if qstate else False,
            queries=[self.queries[q].record for q in self.query_order],
            tool_calls=self.tool_calls,
            model=self.model_info,
            usage=usage,
            cost=CostInfo(
                estimated_usd=estimate_cost_usd(model_for_cost, usage) if self.a.provider.live else 0.0,
                pricing_version=pricing_version() if self.a.provider.live else "test-double (no cost)",
            ),
            latency_ms=round((time.perf_counter() - self.t0) * 1000.0, 1),
            steps=self.steps,
            sql_repair_attempts=self.failed_queries,
            invalid_tool_calls=self.invalid_calls,
            error=error,
            prompt_version=PROMPT_VERSION,
            prompt_hash=self.a.prompt_hash,
        )
        return AgentResult(resp, cols, rows, canonical_result_hash(cols, rows) if qstate else None)

    def _fail(self, exc: _Abort) -> AgentResult:
        d = exc.detail
        if d.code == "context_source_unavailable":
            msg = "The context/data service is unavailable, so no reliable answer can be given."
        else:
            msg = f"The request could not be completed ({d.code}). No answer is given."
        return self._build(Outcome.EXECUTION_ERROR, msg, error=d)

    # ---------------------------------------------------------------- main loop
    async def execute(self) -> AgentResult:
        q = self.question.strip() if isinstance(self.question, str) else ""
        if not q or len(q) > MAX_QUESTION_CHARS:
            return self._build(
                Outcome.CLARIFICATION_REQUIRED,
                f"Please ask a business question of 1-{MAX_QUESTION_CHARS} characters.",
                error=ErrorDetail(
                    code="invalid_question", message="empty or over-long question", stage="input"
                ),
            )
        try:
            session = self.a.provider.start(SYSTEM_PROMPT, self.a.tool_specs)
            parts: list[TextPart | ToolResultPart] = [TextPart(user_message(q))]
            nudges = 0
            bad_submits = 0
            while True:
                if self.steps >= self.cfg.max_steps:
                    raise _Abort("max_steps_exceeded", f"exceeded {self.cfg.max_steps} model steps", "loop")
                self.steps += 1
                try:
                    turn: ModelTurn = await self._bounded(
                        session.send(parts),
                        min(self.cfg.request_timeout_seconds * 4, self._remaining()),
                        "model",
                    )
                except ProviderError as exc:
                    raise _Abort(
                        f"provider_{exc.kind}", exc.message, "model", retryable=exc.retryable
                    ) from exc
                self._account(turn)
                if self.usage.total_tokens > self.cfg.max_total_tokens:
                    raise _Abort("token_budget_exceeded", f"used {self.usage.total_tokens} tokens", "model")
                if turn.stop_reason == "refusal":
                    raise _Abort("model_refusal", f"model declined: {turn.stop_details}", "model")
                if turn.stop_reason == "max_tokens":
                    raise _Abort(
                        "output_truncated", "model output hit max_tokens; tool input may be partial", "model"
                    )
                if not turn.tool_calls:
                    nudges += 1
                    if nudges > 2:
                        raise _Abort(
                            "no_final_response", "model stopped without calling submit_response", "loop"
                        )
                    parts = [TextPart("Finish by calling the submit_response tool (exactly once).")]
                    continue
                results: list[TextPart | ToolResultPart] = []
                final: SubmitArgs | None = None
                for call in turn.tool_calls:
                    if call.name == SUBMIT_TOOL_NAME:
                        sub, err = self._validate_submit(call)
                        rec = ToolCallRecord(
                            step=self.steps, tool=call.name, arguments=call.arguments, ok=sub is not None
                        )
                        self.tool_calls.append(rec)
                        if sub is None:
                            rec.invalid, rec.error_code = True, "invalid_final_response"
                            self.invalid_calls += 1
                            bad_submits += 1
                            if bad_submits > 2:
                                raise _Abort("invalid_final_response", err or "invalid submit", "finalize")
                            results.append(ToolResultPart(call.id, self._encode({"error": err}), True))
                        else:
                            final = sub
                        continue
                    results.append(await self._dispatch(call))
                if final is not None:
                    return self._build(final.outcome, final.answer, sub=final)
                parts = results
        except _Abort as exc:
            return self._fail(exc)
        except McpUnavailable as exc:
            return self._fail(_Abort("context_source_unavailable", str(exc), "tool"))
