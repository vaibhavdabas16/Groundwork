# AGENTS.md - instructions for AI coding agents (Claude Code and others)

Read this before changing anything. Also read `docs/IMPLEMENTATION_STATUS.md` (what is real vs
blocked) and `docs/PHASE_1_HANDOFF.md`.

## Architecture invariants (do not break)
1. **One entry point.** CLI, MCP and future Slack/eval code call `service.core.GroundworkService`.
   Nothing else may call `QueryExecutor`, `validate_sql`, or open the serving DB for agent requests.
2. **Identity is server-side.** The principal comes from process configuration (later, an
   authenticated transport). Never add identity, role or capability fields to tool arguments.
   `RequestContext` stays `extra="forbid"`.
3. **Every tool needs a capability** in `context/policies/policy.yaml: tool_permissions`. Unmapped
   tools are denied.
4. **SQL execution path:**
   `validate_sql` (AST, caller catalog) → regenerated SQL with row cap → `QueryExecutor` (killable
   subprocess) → `worker.py` (read-only, `enable_external_access=false`, `lock_configuration=true`).
   - Never execute caller-supplied SQL text directly.
   - Never weaken worker config.
   - Never use regex as the security boundary.
5. **Serving DB never contains restricted or PII fields.** New sensitive source columns must be
   excluded or pseudonymised in `warehouse/build.py: SERVING_SQL` and classified in `context/assets/`.
6. **Remote MCP stays disabled** until OAuth token verification, a token-to-principal mapping and
   tests exist.
7. **No stdout writes in the MCP server process.** Log through `observability.logging` (stderr, JSON).
8. **Agent data access goes through MCP only.** `groundwork.agent` never opens a database and never
   imports `groundwork.eval` or reads `evals/` (unit-tested). Gold labels must stay invisible to it.
9. **Context modes are enforced server-side.** `GROUNDWORK_CONTEXT_MODE` is fixed per MCP server
   process; restricted modes are filtered in `GroundworkService` / `SearchIndex`. Any new tool,
   field, warning or error text must be checked by `tests/integration/test_context_modes.py`.
10. **No silent fallbacks.** The provider is explicit (`--provider` / `GROUNDWORK_PROVIDER`:
    `anthropic` or `openai`). Missing provider credentials -> BLOCKED (exit 3). Never fall back to the
    scripted test double, another provider or another model; never send server-side model fallbacks.

11. **Phase 3 product layer** (`groundwork.product`) never opens the warehouse for answering (the only exception is read-only `information_schema`
    introspection in `proposals/patches.py` to check that cited columns exist)
    and never trusts identity from message text. Identity comes only from the validated Slack envelope.
    Authorization comes only from `config/product.yaml`, with reviewers mapped to non-placeholder humans.
12. **Proposals change context only through typed operations** (`proposals/models.py`) on
    allow-listed paths (`proposals/patches.py: ALLOWED_PATTERNS`). Never widen the allow-list to
    ownership, policy, evals, code, CI, config or deployment files.
13. **`APPROVED` is reachable only via `ProposalStore.decide`** by an authorized human. A Slack approval
    does not merge. Deployment uses merge commits on the base branch only.
14. **The audit log is append-only.** Never UPDATE/DELETE `audit_events` or drop its triggers. Every
    state change appends its event in the same transaction.

## Governance invariants
- AI agents may create or modify `draft` context. They must **never** set `status: verified`, fill
  `certified_by`, or add `can_certify` to a non-human. Validation enforces this; do not loosen it.
  See `docs/certification_procedure.md`.
- Do not invent business definitions. Every metric needs:
  - SQL that executes against `analytics.*`;
  - an explicit grain;
  - a `double_counting_check`;
  - assumptions and evidence.

  If the data cannot support a concept, add it to `context/unsupported/`.
- Data-quality violations are documented, never "fixed" by altering raw data.
  `data/manifests/dq_baseline.json` changes only when upstream data changes, and the reason goes in the commit.

## Evaluation invariants (Phase 2)
- Never fabricate, edit or "estimate" evaluation results. `docs/EXPERIMENT_RESULTS.md` reports only
  real runs (run id, model ids, snapshot id, prompt hash) or states BLOCKED.
- Expected results come only from `groundwork eval gold freeze` (gold SQL on the snapshot).
- Held-out cases are locked. Do not tune prompts/agent against held-out results. Any label change
  needs `eval gold lock --reason ...` (logged to `evals/LABEL_CHANGELOG.md`).
- Agents may author cases and record AI cross-checks, but never `eval gold review` (human only).
- Scoring rules and `evals/regression_policy.yaml` changes after results exist must be logged with
  justification in `docs/EVALUATION_METHODOLOGY.md`.
- The scripted provider is a test double: tests and `kind=plumbing_test` only.

## Coding conventions
- Python 3.12, Pydantic v2 models (`extra="forbid"` for external input), full type hints.
  `uv run mypy` (strict) must pass.
- `uv run ruff check src tests` and `uv run ruff format src tests`.
- Small modules with explicit dependencies. No agent frameworks.
- Verify third-party APIs against the **installed** version. The `mcp` SDK is **v2**
  (`mcp.server.mcpserver.MCPServer`, snake_case result fields). v1 tutorials using `FastMCP` will not work.

## Testing standards
- `tests/unit`: no dataset needed; fast; uses fixtures and copies of `context/`.
- `tests/integration`, `tests/security`, `tests/mcp`: use the **real** warehouse. If it is missing
  they FAIL with instructions (they do not skip).
- MCP behaviour must be tested through the real stdio transport (`tests/mcp`), not only by calling
  Python functions.
- Never weaken an assertion or change expected values to make a broken implementation pass. If a
  test's *premise* was wrong, fix it and say why in the commit.
- Fault-injection tests (sending unvalidated SQL straight to the worker) are allowed and are labelled.
- Run before finishing: `uv run ruff check src tests && uv run mypy && uv run groundwork validate-context && uv run groundwork eval gold check && uv run pytest -q`.
- `tests/agent` use the real MCP server and warehouse with a scripted model (fault injection); `tests/live` need the chosen provider's key (`OPENAI_API_KEY` with `GROUNDWORK_PROVIDER=openai`, or `ANTHROPIC_API_KEY`) and skip as BLOCKED without it. Never add guessed prices to `agent/pricing.py`; OpenAI prices come only from an operator-supplied `GROUNDWORK_PRICES_FILE`.

## Product invariants (Phase 3)
- Never report Slack, GitHub, model, deployment or user activity as verified without a real interaction.
  Contract tests and the scripted model are test doubles and must stay labelled.
- Infrastructure, provider and permission failures must never produce context proposals.
- Answer approval (shadow mode) and context-change approval stay separate workflows.

## Keep docs truthful
Update `docs/IMPLEMENTATION_STATUS.md` whenever something becomes real, blocked or unverified.
Never claim a check passed unless you ran it in this environment.
