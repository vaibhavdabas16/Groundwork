# Phase 2 handoff: context-aware data agent and evaluation platform

Machine-readable summary: [`docs/phase2_handoff.json`](phase2_handoff.json).

## TL;DR
- **Agent** (`src/groundwork/agent/`): a working LLM agent built on the Anthropic Messages API (SDK 2.11.0). It works only through the Phase 1 MCP server, so all SQL goes through the governed guard and the read-only worker. It returns a structured `AgentResponse` with one of six outcome types.
- **Evaluation platform** (`src/groundwork/eval/`, `evals/`):
  - 90 cases with gold SQL executed on the frozen snapshot;
  - three isolated context conditions, filtered at the server;
  - deterministic execution-based scoring;
  - question-level paired statistics;
  - JSON, Markdown and CSV reports;
  - a regression gate, and CI workflows that cannot turn green without a live run.
- **Provider:** the operator chose **OpenAI**. An OpenAI adapter (`--provider openai`, default model `gpt-5.5`) sits alongside the Anthropic one; both are verified at SDK/wire level only.
- **Live results: BLOCKED.** No `OPENAI_API_KEY` is set in this environment. No accuracy, context-improvement, latency or cost figures exist. See `EXPERIMENT_RESULTS.md`.
- **Gold labels:** 0 are human-reviewed. 71 have SQL-validated, frozen expected results; the held-out split is locked; one independent AI cross-check was done.

## Phase 1 re-verification (done first)
All Phase 1 checks were re-run before building:
- ruff, mypy, validate-context and health all pass;
- 186/186 tests pass;
- the MCP server was started over real stdio.

Defects found and fixed while building Phase 2:
1. **SQL guard rejected allow-listed functions.** `dayofweek`, `dayofyear` and `dayofmonth` were rejected because SQLGlot renames them (`DAY_OF_WEEK`, ...). The normalised names were added to the policy allow-list and the guard defaults, with a regression test.
2. **Domain facts leaked from the guard.** The `CURRENT_DATE` warning named the dataset's end year in every mode. The guard text is now generic; the dataset note is added only in `full_context`.
3. **Snapshot identity was deployment-specific.** Gold identity used the serving-DB file hash, which depends on the pseudonym key. It now uses a content-based `snapshot_id` (source checksums plus build code).

`percentile_cont ... WITHIN GROUP` stays unsupported by the guard; use `quantile_cont` or `median`. This is documented, not changed.

## What exists

| Area | Files |
|---|---|
| Context modes | `config/settings.py: ContextMode`; filtering in `service/core.py` and `search/index.py`; mode-neutral MCP instructions |
| Provider abstraction | `agent/providers/base.py`, `registry.py`, `anthropic_provider.py`, `openai_provider.py`, `scripted.py` (test double only) |
| Runtime | `agent/runtime.py`, `prompts.py`, `schemas.py`, `mcp_client.py`, `factory.py` |
| Tracing and provenance | `agent/traces.py` (SQLite), `agent/provenance.py`, `agent/pricing.py` |
| Gold lifecycle | `eval/cases.py`, `eval/gold.py`; `evals/cases/*.yaml`, `evals/expected/*.json`, `evals/manifest.json`, `evals/heldout_lock.json`, `evals/reviews.yaml`, `evals/LABEL_CHANGELOG.md` |
| Scoring | `eval/compare.py`, `eval/scoring.py`, `eval/sensitive.py` |
| Harness, statistics, reports | `eval/harness.py`, `eval/store.py`, `eval/stats.py`, `eval/report.py` |
| Gate | `eval/gate.py`, `evals/regression_policy.yaml` |
| CLI | `agent_cli.py`: `ask`; `eval run`, `report`, `compare`, `replay`, `list`, `gate`; `eval gold freeze`, `check`, `status`, `lock`, `verify`, `review` |
| CI | `.github/workflows/ci.yml` (deterministic, no secrets), `.github/workflows/live-eval.yml` (trusted code only, protected environment) |
| Tests | `tests/unit/test_anthropic_provider.py`, `tests/unit/test_eval_core.py`, `tests/integration/test_context_modes.py`, `tests/agent/test_agent_runtime.py`, `tests/agent/test_eval_harness.py`, `tests/mcp` (mode test added), `tests/live/test_live_agent.py` |

## Real model identifiers
- **Configured default:** `claude-opus-5-5`, provider `anthropic`, effort `medium`, adaptive thinking, no server-side fallbacks. Any model id can be passed with `--model`.
- **Resolved model ids:** none recorded. No live call was made.

## Acceptance criteria

| # | Criterion | State | Evidence |
|---|---|---|---|
| 1 | A real model answers questions through the actual MCP server | **BLOCKED** (credentials) | The path is implemented and tested end to end with a test double plus SDK contract tests; `tests/live` exists |
| 2 | SQL is generated dynamically, not by question matching | Implemented, structurally verified | There is no question-to-SQL code path; SQL comes only from model tool calls. Not live-verified. |
| 3 | SQL executes through the secured query layer | VERIFIED | Runtime tests; audit-log correlation |
| 4 | Answers expose SQL and context provenance | VERIFIED (runtime) | `test_answer_with_provenance_and_trace` |
| 5 | The agent distinguishes answer, clarification and abstention | Runtime VERIFIED; model behaviour **BLOCKED** | Outcome validation tests |
| 6 | All three context configurations are isolated and testable | VERIFIED | Leakage probes; real-stdio interface test |
| 7 | 80–100 meaningful evaluation cases exist | VERIFIED | 90 cases; distribution test |
| 8 | Each gold SQL executes on the real dataset | VERIFIED | `eval gold check`: 81/81 |
| 9 | Gold labels have genuine review status | VERIFIED (truthful) | Status shows 0 human-reviewed; the review workflow requires a human owner |
| 10 | Live attempts are recorded, not fabricated | Implemented; **no live attempts exist** | Store and traces; test-double runs are labelled |
| 11 | Correctness uses real result comparisons | VERIFIED | Comparator unit tests; plumbing run scored against frozen gold |
| 12 | Three-repetition evaluation is implemented | Implemented, plumbing-tested (2 reps) | `--repetitions 3`; question-level statistics |
| 13 | Latency, consistency and cost are recorded | Implemented | Live values are BLOCKED |
| 14 | Context improvement is calculated from real results | **BLOCKED** | Calculation implemented and unit-tested |
| 15 | Failed and incomplete runs stay visible | VERIFIED | Budget-exhaustion test → INCOMPLETE; harness errors recorded as attempts |
| 16 | Regression checks are operational | Deterministic checks operational; live gate returns BLOCKED (exit 3) | `eval gate` |
| 17 | Security restrictions remain intact | VERIFIED | Full Phase 1 security suite plus new injection tests pass |

**Phase 2 is NOT complete.** Live evaluation and human gold review are outstanding.

## Test commands executed (this environment)
```
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy
uv run groundwork validate-context && uv run groundwork health
uv run groundwork eval gold freeze | check | lock | verify | status
uv run pytest -q          # see IMPLEMENTATION_STATUS.md for the final count
uv run groundwork ask "How many orders were canceled?"   # -> BLOCKED, exit 3
uv run groundwork eval gate                               # -> BLOCKED, exit 3
```

## Remaining blockers
1. **Provider credentials.** Set `GROUNDWORK_PROVIDER=openai` and `OPENAI_API_KEY` (plus `GROUNDWORK_PRICES_FILE` with verified prices to use `--budget-usd`). Then run the commands in `EXPERIMENT_RESULTS.md`.
2. **Human review of gold.** Register real reviewers in `owners.yaml` and review held-out cases with `eval gold review`. At least 50 are needed for the gate.
3. **Human certification of context.** This is a Phase 1 carry-over: every governed definition is still a DRAFT.
4. **Unverified infrastructure.** Docker, make and GitHub Actions are unverified here: the tools are not installed and there is no remote repository. Branch protection and the `live-eval` environment must be configured by a repository admin.
5. **No git commits** have been made; none were requested. `code_version.git_sha` is therefore null, and the source hash is used instead.

## Phase 3 integration requirements (Slack and the PR workflow)
- **Integration point:**
  - consume `AgentResponse` as-is;
  - render `outcome`, `answer`, `clarification_question`, `assumptions`, `warnings`, `certification_status`, `executed_sql` and `result_preview`;
  - surface the DRAFT and DEPRECATED warnings verbatim.
- **Entry point:** call `open_agent(...)` and then `DataAgent.ask(question, request_id=<slack event id>, cancel_event=...)`. Never call the MCP server or the database directly.
- **Identity:** map each authenticated Slack user to a policy principal server-side. The MCP server's principal comes from process configuration, so run one server per principal, or add authenticated transport first. Remote MCP is still disabled.
- **Correlation:** pass the Slack event id as `request_id`. Traces, the MCP audit log and query ids then join on it.
- **Human-approved PR workflow:** the agent may draft context and gold-label changes. Certification (`certified_by`) and gold review (`eval gold review`) must stay human-only. CI's `gold check`, the lock verification and `live-eval` (via `context_pr`) evaluate a candidate context as data with trusted code.
- **Error handling:** map `error.code` to user messages. `retryable` tells you whether a retry button makes sense.
