# Implementation status (truthful ledger)

Last updated: after Phase 3 (first live OpenAI calls). Environment: Windows 11, Python 3.12.7, uv 0.13.9, duckdb 2.0.0,
mcp 2.3.0, sqlglot 30.33.0, anthropic 2.11.0, openai 3.57.0, slack-bolt 1.30.0, slack-sdk 3.48.0, ruamel.yaml 0.19.1.

Legend:
- **VERIFIED**: executed in this environment, with evidence.
- **IMPLEMENTED, UNVERIFIED**: code or config exists but has not been run here.
- **BLOCKED**: needs something external.
- **NOT STARTED**: planned for a later phase.

| Component | State | Evidence / notes |
|---|---|---|
| Dataset acquisition (Kaggle public endpoint, anonymous) | VERIFIED | Two independent downloads produced identical archive and file SHA-256s (`data/manifests/olist_source_manifest.json`). |
| Manual import (`--zip`, `--dir`) | IMPLEMENTED, UNVERIFIED | Same extraction and checksum code path as download. No dedicated test. |
| License verification | VERIFIED | CC BY-NC-SA 4.0 from the Kaggle metadata API, recorded in the manifest. |
| Raw warehouse (explicit types, zero cast failures) | VERIFIED | `tests/integration/test_dataset.py`. Row counts cross-checked against Python `csv`. |
| Serving warehouse (pseudonymised, no raw sensitive fields) | VERIFIED | `tests/security/test_security.py::test_raw_sensitive_values_physically_absent_from_serving_db`. |
| Ingestion repeatability | VERIFIED | `test_ingestion_is_repeatable` (row hashes identical across rebuilds). It caught and drove fixes for two non-deterministic aggregates. |
| Data-quality checks and report | VERIFIED | 21/21 invariants pass; 32 observations match the committed baseline. `docs/data_quality_report.md`. |
| Context schemas, loader, validation | VERIFIED | 0 errors, 0 warnings on the real context; 26 mutation tests reject bad content. |
| Business context content | VERIFIED as executable, **NOT certified** | All metric SQL executes through the governed path and matches direct DuckDB. Everything is `draft` (or `deprecated`); owners are placeholders. |
| Certification enforcement | VERIFIED | AI/agent self-certification, placeholder owners, missing fields and draft-with-certifier are rejected. A complete human certification is accepted (positive control). |
| BM25 search with visibility rules | VERIFIED | `tests/unit/test_search_and_policy.py`. |
| Policy engine / capabilities / principals | VERIFIED | Unit, security and MCP tests. |
| SQL guard (SQLGlot AST) | VERIFIED | 55 unit tests and 39 real-warehouse rejection tests. |
| Locked read-only DuckDB worker | VERIFIED | Fault-injection tests send unvalidated attacks straight to the worker. |
| Hard timeout (process kill) | VERIFIED | Cross join killed at ~2 s. |
| Row, byte and concurrency limits; tool-call budget | VERIFIED | Security tests. |
| POSIX RLIMIT_AS / RLIMIT_CPU in the worker | IMPLEMENTED, UNVERIFIED | Not applicable on Windows. Will run on Linux CI. |
| Audit log (SQLite, retention purge) | VERIFIED (write path) | `test_audit_log_records_calls_without_rows`. Purge-by-age is not separately tested. |
| MCP server (stdio, official SDK v2) | VERIFIED | `tests/mcp` spawns the real server: discovery, context tools, query validated against DuckDB, invalid args, structured errors, clean close. |
| MCP HTTP transport | BLOCKED by design | Refused (exit 2) until authentication is implemented (tested). |
| CLI | VERIFIED | acquire, build, quality, validate-context, search, define, query, health were run. |
| From-zero install following README | VERIFIED (Windows/uv) | Clean copy: `uv sync --frozen` → acquire → build → quality → validate → 186 tests passed. |
| Makefile | IMPLEMENTED, UNVERIFIED | `make` is not installed here. Targets only wrap the verified `uv run` commands. |
| Dockerfile / docker-compose | IMPLEMENTED, UNVERIFIED | Docker is not installed here. |
| GitHub Actions CI | IMPLEMENTED, UNVERIFIED | Not run. No remote repository was configured or pushed. |
| Lint / format / types | VERIFIED | `ruff check`, `ruff format --check`, `mypy --strict`: clean. |
| Git history | NOT STARTED | `git init` was run; **no commits were made** (not requested). `context_version` therefore uses the content hash only (`ctx-<sha256[:12]>`); it appends `+git.<sha>` once committed. |
| Slack / human-approved PR workflow (Phase 3) | NOT STARTED | Out of scope. |

## Phase 2

| Component | State | Evidence / notes |
|---|---|---|
| Phase 1 re-verification | VERIFIED | 186/186 tests, lint, types, validate-context, health re-run before Phase 2 work. |
| Phase 1 fixes: guard day-function aliases, generic CURRENT_DATE warning, content-based snapshot id | VERIFIED | Regression tests in `tests/unit/test_sql_guard.py`, `tests/integration/test_context_modes.py`. |
| Context modes (raw_schema / descriptions / full_context), server-side | VERIFIED | Exhaustive leakage probe; identical tool schemas over real stdio (`tests/mcp`). |
| Provider abstraction + Anthropic adapter | VERIFIED at SDK/wire level | 11 contract tests run the real `anthropic` SDK against an HTTP mock transport (no network). **Not live-verified.** |
| OpenAI adapter (Responses API, stateless `store=false` + encrypted reasoning replay, `--provider openai`, default model `gpt-5.5`) | **LIVE-VERIFIED (smoke)** | Moved from Chat Completions after the first live call returned HTTP 400 (gpt-5.5 rejects function tools + `reasoning_effort` on `/v1/chat/completions`). 19 contract tests run the real `openai` 3.57.0 SDK against an HTTP mock transport; `tests/agent/test_openai_agent_wire.py` drives it through the real runtime + MCP server and checks the item sequence. LIVE-VERIFIED: `tests/live/test_live_agent.py` passed against real OpenAI `gpt-5.5` (resolved `gpt-5.5-2026-04-23`); one `ask` (trace `tr-e45fdbcf8a194f9eadbce509456f269d`) answered 6,824 via real MCP + DuckDB, matching gold `cf-017`. This is a smoke check, not an evaluation. No OpenAI prices are built in: cost is reported as unknown unless `GROUNDWORK_PRICES_FILE` supplies operator-verified prices, and a `--budget-usd` run on an unpriced model is BLOCKED. |
| Live model calls (`ask`, `eval run`) | `ask`: **LIVE-VERIFIED (smoke)**; `eval run`: **INCOMPLETE** | `OPENAI_API_KEY` provided by the operator after Phase 3. The full experiment `run-20261121T100535-4bc95e` was stopped by the operator at 24/630 attempts (budget limit); it is not a result. no OpenAI prices are configured, so cost is reported as unknown. |
| Agent runtime (limits, validation, structured outcomes, cancellation, tracing, MCP-only data access) | VERIFIED with real MCP + governed SQL and a scripted test double | 14 tests in `tests/agent/test_agent_runtime.py` (fault injection, labelled as such). |
| Evaluation dataset (90 cases, 20 dev / 70 held-out) | VERIFIED (authored + SQL-validated) | `eval gold check`: 81/81 gold queries re-execute identically. |
| Gold human review | **BLOCKED** | 0 human-reviewed; no real reviewers exist in `owners.yaml`. One AI cross-check recorded (not counted). |
| Held-out lock | VERIFIED | `evals/heldout_lock.json`; re-lock history in `evals/LABEL_CHANGELOG.md`. |
| Scoring / comparator / statistics | VERIFIED (unit) | `tests/unit/test_eval_core.py`. |
| Harness (concurrency, budget, INCOMPLETE, artifacts, replay, compare) | VERIFIED with test double (plumbing) | `tests/agent/test_eval_harness.py`; `replay`/`compare` CLI not exercised against a live run. |
| Experiment results / context improvement | **BLOCKED** | No live run; `docs/EXPERIMENT_RESULTS.md` contains no numbers. |
| Regression gate | VERIFIED (BLOCKED path) | `eval gate` -> BLOCKED exit 3 with no live run; unit-tested. PASS/FAIL paths need a live reviewed run. |
| `live-eval.yml` workflow, CI gold check step | IMPLEMENTED, UNVERIFIED | No remote repository / GitHub Actions here. |
| Tests overall | VERIFIED | Phase 2, after adding the OpenAI provider, run in three chunks (an earlier all-in-one run was stopped by the host for low memory): `tests/unit` 141 passed; `tests/agent tests/mcp tests/live` 24 passed, 1 skipped (live test, BLOCKED: no key); integration + security 88 passed. Total **253 passed, 1 skipped**. ruff, format and mypy clean. |



## Phase 3 (Slack product, improvement loop, governance, deployment)

Completion levels are kept separate. Details: `docs/PHASE_3_HANDOFF.md`.

| Component | State | Evidence / notes |
|---|---|---|
| Phase 0 re-verification | VERIFIED | 253 passed / 1 skipped before changes. MCP over real stdio. Repo has no commits. |
| Phase 2 defect: provenance crashed on a missing warehouse | FIXED | `test_database_outage_is_reported_honestly_*` |
| Slack app (Bolt; Socket Mode + signed HTTP; DM, mentions, threads, buttons, modals) | IMPLEMENTED, CONTRACT-TESTED | `tests/unit/test_slack_bolt_contract.py`: real Bolt with HMAC-signed requests, real slack_sdk against a mock Web API. **Not live-verified** (no Slack credentials). |
| Product flows (dedupe, allow-list, consent, clarification, shadow review, delivery failure, restart) | INTEGRATION-TESTED | `tests/agent/test_product_slack_flow.py`: real runtime, MCP and DuckDB with a **scripted model** and a recording gateway |
| Failure taxonomy + classifier | VERIFIED (unit) | 13 parametrised cases + 2 non-failure cases; infra failures are never context-fixable |
| Improvement engine (grounded drafts, human-decision routing) | INTEGRATION-TESTED (scripted drafter) | `tests/integration/test_product_proposals.py`. Live drafting not yet run. |
| Proposal lifecycle + reviewer authorization | VERIFIED (unit) | Concurrency, replay, stale revision, self-approval, elevated tier |
| Proposal validation (before/after × N + regression) | IMPLEMENTED; static and BLOCKED paths tested | Live comparison needs a model; the harness refuses test doubles |
| GitHub integration | CONTRACT-TESTED | `tests/unit/test_github_contract.py`, `tests/integration/test_product_workflow.py` (REST fake). **0 real PRs** (no credentials, no remote). |
| Durable audit (append-only + hash chain) | VERIFIED | Trigger and tamper-detection tests; backup verifies the chain |
| Context releases (validate, atomic switch, verify, rollback) | VERIFIED locally | Release `ctx-1241c1f404c2` deployed to the local ops DB. The git-commit deploy path is unexercised (no commits). |
| Ops endpoints, metrics, pilot report | VERIFIED locally | `tests/integration/test_product_ops.py` |
| Shadow mode | INTEGRATION-TESTED | Requester and non-reviewer approvals refused; replay idempotent |
| Docker Compose deployment, backup script, Slack manifest | IMPLEMENTED, **UNVERIFIED** | Docker not installed |
| CI: secrets scan, dependency audit, proposal-scope, product-live | IMPLEMENTED, **UNVERIFIED** | No GitHub Actions run. The scope checker is tested against a real temporary git repo. |
| Branch protection / protected environments | **NOT CONFIGURED** | Needs a repository admin; no remote exists |
| Real users / approvals / PRs / deployments | **NONE** | `docs/PILOT_RESULTS.md`: "Pilot prepared; no verified user activity yet." |
| Tests overall | VERIFIED | End of Phase 3, three chunks: `tests/unit` 187 passed; `tests/agent tests/mcp tests/live` 49 passed, 3 skipped (BLOCKED: model, Slack, GitHub); `tests/integration tests/security` 105 passed. **Total 341 passed, 3 skipped.** ruff, format, mypy strict, validate-context, gold check 81/81 and gold verify are clean. |

## Rename to Groundwork
- **Scope:** the project was renamed from ContextForge to Groundwork. That covers the package
  (`src/groundwork`), the CLI (`groundwork`), the environment variables (`GROUNDWORK_*`, which
  replace `CONTEXTFORGE_*`), the MCP server name, the policy id (`policy.groundwork.default`) and
  the docs.
- **Unchanged:** `evals/` (locked) and `data/manifests/` (snapshot inputs).
- **Snapshot id:** the snapshot id covers the warehouse build source, so the rename changed it.
  Gold SQL was re-executed with `eval gold freeze`; all rows and columns were identical, and the
  held-out set was re-locked with that reason (see `evals/LABEL_CHANGELOG.md`).
- **Deployment defect found and fixed:** once the repository had git commits, every
  `context deploy` failed verification. The source context version carries a `+git.<sha>` suffix
  that a copied release cannot reproduce, and the pointer had already switched before
  verification. Releases are now named by content (`ctx-<hash>`). The staged copy is verified
  before the switch, and the previous release is restored if post-switch verification fails.
  Regression test: `test_deploy_from_git_committed_context_uses_content_version`.
- **Tests after the rename:** one full run, without a model key: 342 passed, 3 skipped (BLOCKED: model, Slack, GitHub). ruff, mypy (strict), `validate-context`, `eval gold check` (81/81) and `eval gold verify` are clean.
