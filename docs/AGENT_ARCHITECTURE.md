# Agent architecture (Phase 2)

Status: **implemented and tested with the real MCP server, real governed SQL, and the real Anthropic SDK
(wire-level contract tests).** Live OpenAI calls have been smoke-verified (`tests/live` passed); no evaluation run
exists yet (see `docs/EXPERIMENT_RESULTS.md`).

## Request path

```
question ──► DataAgent.ask()  (src/contextforge/agent/runtime.py)
              │  deterministic loop: steps ≤ max_steps, SQL failures ≤ max_sql_repairs,
              │  tokens ≤ max_total_tokens, wall clock ≤ deadline, cooperative cancellation
              ├─► ModelProvider / ProviderSession  (agent/providers/)
              │     anthropic_provider.py  official `anthropic` SDK 2.11.0, Messages API, tool use
              │     scripted.py            TEST DOUBLE only (never selectable from the CLI)
              └─► McpToolClient (agent/mcp_client.py) ── stdio ──► Phase 1 MCP server process
                                                                   CONTEXTFORGE_PRINCIPAL, CONTEXTFORGE_CONTEXT_MODE
                                                                   └─► ContextForgeService
                                                                        ├─ policy / capabilities
                                                                        ├─ context-mode filtering
                                                                        ├─ SQLGlot AST guard
                                                                        └─ killable read-only DuckDB worker
```

The agent has exactly one route to data: MCP → `ContextForgeService` → SQL guard → worker. The
`contextforge.agent` package opens no database connection (enforced by review and by
`tests/unit/test_eval_core.py::test_agent_code_cannot_see_labels`, which also forbids imports of the
evaluation package).

## Stages and who performs them

| Stage | Performed by |
|---|---|
| Validate question (empty / > 2000 chars → CLARIFICATION_REQUIRED, no model call) | runtime (deterministic) |
| Decide clarification, identify concepts, plan, choose assets/joins, write SQL, interpret results, write answer | LLM |
| Retrieve context (search_assets, get_asset, get_definition, get_join_path, get_policy) | LLM chooses; MCP server executes and filters by mode |
| Permission enforcement | MCP server policy engine (principal fixed at server start) + runtime tool allow-list |
| SQL validation, execution, limits | MCP server (`explain_query`, `run_query`) |
| Certification status, referenced assets, executed SQL, result preview | runtime, from recorded tool results only |
| Final outcome | LLM proposes via `submit_response`; runtime validates (schema, referenced `query_id` must be a successful query of this request, clarification needs a question; at most 2 corrections) |

## Response contract (`agent/schemas.py: AgentResponse`)

Consumed by the CLI and evaluation harness today and by the Phase 3 Slack integration. Fields:
`request_id/trace_id`, `outcome` (ANSWER, CLARIFICATION_REQUIRED, INSUFFICIENT_CONTEXT,
UNSUPPORTED_QUESTION, POLICY_DENIED, EXECUTION_ERROR), `question`, `answer`, `clarification_question`,
`answer_basis` (query_result | metadata | none), `generated_sql` (as written by the model),
`executed_sql` (as regenerated and capped by the guard), `query_id`, `referenced_assets/metrics/terms`
(only ids actually returned by tools; others are dropped with a warning), `context_version`,
`context_mode`, `certification_status` (verified | draft | deprecated_used | mixed | not_exposed | none),
`assumptions`, `warnings`, `result_columns`, `result_preview` (≤ 10 rows), `result_row_count`,
`result_truncated`, `queries[]` (every run_query attempt with status and timings), `tool_calls[]`
(arguments + result summaries, never rows), `model` (provider, requested model, resolved model ids per
call, parameters, provider request ids, `live` flag), `usage` (input/output/cache tokens, model calls,
provider retries), `cost` (estimate + pricing version), `latency_ms`, `steps`, `sql_repair_attempts`,
`invalid_tool_calls`, `error` (code, message, stage, retryable), `prompt_version`, `prompt_hash`.

Failures are never collapsed into free text: each maps to a structured `error.code`, e.g.
`provider_rate_limited`, `provider_auth`, `model_timeout`, `model_refusal`, `output_truncated`,
`max_steps_exceeded`, `token_budget_exceeded`, `deadline_exceeded`, `cancelled`, `no_final_response`,
`invalid_final_response`, `context_source_unavailable`, `invalid_question`.

## Model provider abstraction

`ModelProvider.start(system, tools) -> ProviderSession`; `ProviderSession.send(parts) -> ModelTurn`.
Each adapter keeps the provider-native message history append-only (thinking blocks are replayed
unchanged), so the runtime is vendor-neutral. `registry.make_provider` is explicit: unknown provider →
error; missing credentials → `ProviderNotConfigured` → callers report **BLOCKED (exit 3)**. There is no
fallback to another provider, model or test double.

### Anthropic adapter (the implemented live provider)

Verified against the installed SDK (`anthropic==2.11.0`, API version header `2026-10-21`):

| Requirement | Implementation |
|---|---|
| Explicit config / model id | `AgentConfig.provider`, `AgentConfig.model` (default `claude-opus-5-5`) |
| Credentials | only `ANTHROPIC_API_KEY` (explicit); SDK implicit credential discovery is not used |
| Structured tool calling | Messages API `tools` + `tool_use`/`tool_result`; arguments validated client-side |
| Timeouts | per-request SDK timeout; per-step and per-request deadlines in the runtime |
| Rate limits / retries | SDK retries disabled; adapter retries 429/529/5xx/connection/timeout up to `max_provider_retries`, honouring `retry-after`; non-retryable 400/401/403/404 fail fast |
| Token usage | `usage.input_tokens/output_tokens/cache_creation_input_tokens/cache_read_input_tokens` per call |
| Response validation | stop reasons `refusal` and `max_tokens` are errors (tools from a truncated turn are never run); tool input must be an object |
| Temperature | configurable field, but the installed SDK/API version rejects sampling parameters on every model, so a set temperature is a configuration error (not silently dropped) |
| Token budgets | `max_output_tokens_per_turn`, `max_total_tokens`; `effort` (low…max) |
| Thinking | `{"type": "adaptive"}` |
| Cancellation | asyncio task cancellation of the in-flight HTTP request via `cancel_event` |
| Model switching | no server-side `fallbacks` parameter is sent (an evaluation must not switch models); the provider-reported model id of every call is recorded and a mismatch is warned |
| Prompt caching | top-level ephemeral cache on the input prefix (configurable). It never reuses responses. |

### OpenAI adapter (`providers/openai_provider.py`, the operator's chosen provider)

Verified against the installed SDK (`openai==3.57.0`) with mock-transport contract tests and a
runtime + MCP wire test. Not live-verified.

| Requirement | Implementation |
|---|---|
| Selection | `--provider openai` or `CONTEXTFORGE_PROVIDER=openai`; `--model` (default `gpt-5.5`, an id listed by the installed SDK). A `claude-*` model with this provider is a config error |
| Credentials | only `OPENAI_API_KEY`; SDK workload identity, admin keys and provider integrations are not used |
| Endpoint | `base_url` passed explicitly (default `https://api.openai.com/v1`); an ambient `OPENAI_BASE_URL` is ignored (tested); `CONTEXTFORGE_OPENAI_BASE_URL` overrides deliberately and is recorded |
| API | Responses API with function tools, stateless (`store=false`, no `previous_response_id`); every output item, including reasoning with `reasoning.encrypted_content`, is replayed unchanged; each tool result becomes a `function_call_output` item. Chat Completions was dropped: gpt-5.5 rejects function tools + `reasoning_effort` there (HTTP 400 on the first live call) |
| Retries | SDK retries disabled; adapter retries 429 / 5xx / connection / timeout, honouring `retry-after`; `insufficient_quota`, 400, 401, 403, 404 fail fast |
| Reasoning / sampling | `effort` sent as `reasoning_effort`; `temperature` only if configured. A model that rejects either returns a 400, which is surfaced, not dropped |
| Stop reasons | `tool_calls`->`tool_use`, `stop`->`end_turn`, `length`->`max_tokens`, `content_filter` or a `refusal` message->`refusal` |
| Usage | `prompt_tokens - cached_tokens` -> input; `cached_tokens` -> cache read; `completion_tokens` (incl. reasoning) -> output |
| Storage | `store=False` |
| Cost | no built-in OpenAI prices (never guessed); `CONTEXTFORGE_PRICES_FILE` supplies operator-verified prices (its hash goes into the pricing version); a budgeted run on an unpriced model is BLOCKED |
| Model switching | no fallback; the resolved model id (e.g. a dated snapshot) is recorded per call |

Additional providers are added as independent adapters implementing the same two methods.

## Prompt

One prompt (`agent/prompts.py`, `agent-prompt.v1`) for every experimental condition. It contains no
dataset facts, table names, metric definitions or join rules (unit-tested). The hash of system prompt
+ exact tool specs is stamped on every response and trace (`prompt_hash`); tests assert the hash is
identical in all three context modes.

Untrusted input handling: the question is wrapped as untrusted; every tool output is wrapped as
`{"untrusted_tool_output": ...}`; the prompt forbids following instructions inside them. Independent
of model behaviour, an injected instruction cannot widen access: identity, mode and permissions are
fixed in the server process, and the SQL guard rejects restricted relations and columns (tested with a
test double that deliberately obeys an injection).

## Context modes (experimental conditions)

`CONTEXTFORGE_CONTEXT_MODE` is set when the MCP server process starts (`raw_schema | descriptions |
full_context`); the agent cannot change it. Filtering happens in `ContextForgeService` and
`SearchIndex`, not in prompts:

| | raw_schema | descriptions | full_context |
|---|---|---|---|
| search corpus | serving tables + columns (names, types) | + table/column descriptions | everything (glossary, metrics, joins, unsupported register) |
| get_asset | name, columns (name, type, nullable), queryable columns | + descriptions | full document (grain, keys, quality warnings, lineage, certification) |
| get_definition / get_join_path | `not_available_in_context_mode` | same | available |
| get_policy | access decision + sensitivity class | same | + handling notes, certification |
| run_query warnings / asset refs | guard warnings only; no certification | same | + draft/deprecation notes, dataset coverage note |
| MCP server instructions | neutral text | neutral text | governed-context guidance |
| permissions, SQL guard, limits | identical | identical | identical |

Leakage is tested by probing every tool with every glossary/metric/unsupported name, synonym and id,
every asset, 40 join pairs, policy lookups and valid/invalid/CURRENT_DATE SQL, and checking all string
values against every forbidden context string (`tests/integration/test_context_modes.py`), plus a real
stdio test that tool schemas are identical across modes (`tests/mcp`).

A Phase 1 leak was found and fixed while doing this: the SQL guard's CURRENT_DATE warning named the
dataset's end year; the guard text is now generic and the dataset-specific note is added only in
`full_context`.

## Observability

* Traces: `data/ops/agent_traces.sqlite` (`agent/traces.py`) with trace/request id, eval run/case/attempt
  ids, replay-of, mode, prompt version/hash, code git SHA + source hash, context version, dataset
  snapshot id (+ serving DB file hash), provider, live flag, requested + resolved model ids, model
  parameters, agent config, tool calls, queries (generated/executed SQL, timings, status), result SHA-256
  and row count (never values), references, latency, usage, cost + pricing version, comparison.
* Correlation: the trace id is injected as `request_context.request_id` on every MCP call, so the MCP
  server's audit log (`audit.sqlite`, which records each query id and executed SQL) joins to the trace
  (tested).
* Logging: JSON to stderr; the MCP server never writes to stdout.
