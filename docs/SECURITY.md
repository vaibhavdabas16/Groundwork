# Security model

## Threat model
SQL and tool arguments come from an AI agent and are **untrusted**. Goals:
- no writes;
- no file or network access;
- no access to raw or sensitive fields;
- no resource exhaustion;
- no privilege escalation through arguments;
- no data leaks through errors or logs.

## Trust boundary and identity
- The caller principal is fixed **server-side** at process start (`CONTEXTFORGE_PRINCIPAL`).
- Principals are defined in `context/policies/policy.yaml` and map to roles and capabilities.
- No tool argument carries identity. `request_context` is a closed schema (`extra="forbid"`), so
  `{"role": ...}` is rejected (tested over real MCP).
- **Remote exposure.** The HTTP transport is refused (`serve --transport streamable-http` exits
  with code 2). It stays disabled until all of these are implemented and tested:
  1. OAuth 2.1 resource-server token verification (SDK `TokenVerifier` + `AuthSettings`);
  2. a token-to-principal mapping;
  3. TLS termination;
  4. per-principal rate limits.

## Capabilities
| Capability | Grants |
|---|---|
| `metadata.read` | search_assets, get_asset, get_definition, get_join_path, get_policy |
| `metrics.read_verified` / `metrics.read_draft` | which certification states are visible |
| `query.explain` | explain_query |
| `query.execute` | run_query |
| `context.admin` | validate_context (and future write operations) |

A tool with no permission mapping is denied (fail closed).

## Defence in depth for SQL
1. **AST validation** (`security/sql_guard.py`, SQLGlot, DuckDB dialect). Rejects:
   - more than one statement, or a root that isn't a query;
   - any node type outside an allow-list, including `INTO`, `USING SAMPLE`, time travel and recursive CTEs;
   - functions outside an allow-list (reviewed in the policy; a hard-deny list blocks `read_*`,
     `glob`, `getenv`, `current_setting`, `query*` and more);
   - table functions, file-path relations, catalog-qualified names, and relations not granted to the caller;
   - any column not in the caller's catalog. Qualification against a role-filtered schema covers
     aliasing, subqueries, CTEs, joins, aggregates, string aggregation and `*` expansion;
   - queries over the complexity limits.

   What gets executed is the **regenerated, fully qualified** SQL with a row cap, never the input text.
2. **Physical separation.** The agent-facing worker opens only `olist_serving.duckdb`, which
   contains no raw sensitive fields:
   - `customer_unique_id`, `customer_id` → HMAC-SHA256 pseudonyms;
   - customer ZIP prefix → dropped;
   - review text → booleans;
   - raw geolocation points → per-prefix aggregates.

   A test scans every VARCHAR column for sampled raw identifiers and review texts.
3. **Locked database.** The worker opens the DB with:
   - `read_only=True` and `enable_external_access=false`;
   - extension auto-install, auto-load, community and unsigned extensions disabled;
   - persistent secrets disabled;
   - memory, thread and temp-size limits;
   - `lock_configuration=true`.

   Fault-injection tests send *unvalidated* attacks straight to the worker and confirm they fail.
4. **Process isolation and a hard timeout.**
   - Each query runs in a fresh `python -I -m contextforge.query.worker` process, with a minimal
     environment (no secrets) and a temporary working directory.
   - The parent **kills** the process at the timeout. This is tested with a cross join, killed at
     about 2 s.
   - On POSIX the worker also sets `RLIMIT_AS`/`RLIMIT_CPU`; on Windows, the kill is the bound.
5. **Resource limits.**
   - Row cap (`max_rows + 1` is fetched to detect truncation) and a result byte cap.
   - Concurrency semaphore (non-blocking, so excess queries are rejected).
   - Advisory plan-cardinality cost limit (`EXPLAIN (FORMAT JSON)`).
   - Per-request tool-call budget.
6. **Result boundary.**
   - DuckDB error messages are reduced to their first line, with quoted fragments (paths,
     literal values) redacted.
   - A last-line DLP pass redacts email, CPF/CNPJ and phone patterns.

   This is *not* the primary privacy control: (1) and (2) are.
7. **Audit.** Every tool call goes to `data/ops/audit.sqlite`: request id, principal, tool,
   status, error code, SQL and executed SQL, row count, elapsed time and context version. Result
   rows are never stored. Retention purge runs on startup (`retention.query_audit_log_days`).

## Known limitations
- On Windows there is no OS memory rlimit for the worker; DuckDB's `memory_limit` plus the
  wall-clock kill are the bounds.
- The plan-cost limit is advisory. It relies on DuckDB's cardinality estimates.
- Pseudonymous keys are stable within a deployment and can link rows for the same person; the
  analyst role is allowed this (`max_sensitivity: pseudonymous`).
- The DLP patterns are heuristic.
- A container sandbox (`read_only`, `cap_drop: ALL`, `no-new-privileges`) is declared in
  `docker-compose.yml` but **not verified** (no Docker on the authoring machine).

# Phase 3 additions (Slack product, proposals, GitHub, deployment)

The Phase 1 controls above are unchanged and still enforce every data access. Phase 3 adds external
interfaces; this section lists the controls for each. The threat analysis is in
[`THREAT_MODEL.md`](THREAT_MODEL.md).

## Identity and authorization
| Decision | Source of truth | Enforced in |
|---|---|---|
| Who is asking | `user`/`team` from the validated Slack envelope. **HTTP mode:** Bolt verifies the `X-Slack-Signature` HMAC and timestamp. **Socket Mode:** authenticated WebSocket. | `slack_app.py`, `core.handle_message` |
| May they ask | `slack.allowed_team_ids` + `users` allow-list in `config/product.yaml` | `core.handle_message` |
| What data they may see | Policy principal mapped per user. Each request runs its own MCP server with that principal fixed at start. | Phase 1 policy engine + SQL guard |
| May they approve an **answer** (shadow mode) | `answer_reviewers` (non-placeholder humans in `owners.yaml`); not the requester | `core.decide_answer` |
| May they approve a **context change** | `context_reviewers`, checked for domain coverage, the elevated tier, the current revision and not being the author | `ProposalStore.decide` (backend, inside one transaction) |
| May it merge | GitHub branch protection (required reviews + checks) **and** a ContextForge approval of the validated head SHA | `ProposalWorkflow.check_merge_eligibility` |

Rules that hold across all of these:
- Text inside a message is never identity or authorization. "approved" typed in Slack does nothing (tested).
- Identity and reviewer mappings live outside `context/`, so a proposal can never grant itself rights.

## Proposal containment (what an AI-drafted change can touch)
- **Allow-list.** A proposal can only change:
  - `context/glossary/*.yaml`
  - `context/assets/serving/*.yaml`
  - `context/joins/*.yaml`
  - `context/metrics/*.yaml`
  - `context/unsupported/*.yaml`
- **Explicitly forbidden:**
  - ownership, policies and raw-layer docs;
  - `evals/` (gold answers);
  - `src/`, `tests/`, `.github/`, `config/`, `deploy/`;
  - lock files and Docker files.
- **Checked in three places:**
  1. `patches.check_repo_path` when staging;
  2. `GitHubClient.commit_files` before any blob is created;
  3. the `proposal-scope` CI job, which imports the checker **from the base branch**.
- **Structured operations only.** Proposals are typed patch operations, never free-form file writes.
- **Certification can't be self-granted.** Every new or modified object is `draft`, and an AI edit to a `verified` object drops it back to `draft`. Additions of `status: verified`, `certified_by`, `can_certify` or `certified_at` are rejected by:
  - validation;
  - the static check;
  - CI;
  - deployment (an AI-certified release is rejected; tested).
- **Elevated review.** Metric SQL changes and deprecations are `elevated` and need an elevated reviewer.
- **Grounding.** Every cited evidence item must exist: a real column, a context object, a query id the drafter actually executed in that session, or the originating trace. Fabricated evidence is refused (tested).

## Secrets
- **Where they live.** Tokens come only from environment variables (`deploy/.env`, mode 600, git-ignored). They are never in YAML config, prompts or the database.
- **MCP subprocess.** It receives an allow-listed environment that contains no provider, Slack or GitHub secrets (tested).
- **Outbound Slack scrubbing.** Every outbound payload goes through `ScrubbingGateway`, which removes:
  - configured secret values;
  - token-shaped strings (`xox*-`, `xapp-`, `sk-`, `gh*_`, `github_pat_`).

  This is tested with a simulated leaking model.
- **GitHub.** Prefer a GitHub App. Its installation tokens are requested per repository with `contents:write`, `pull_requests:write`, `issues:write`, `checks:read` and `statuses:read` only. The client has no merge capability unless governance enables `auto_merge`.

## Durable audit
- **Append-only.** `audit_events` in `data/ops/product.sqlite`. SQLite triggers abort UPDATE and DELETE.
- **Hash chain.** Each event stores `sha256(prev_hash | canonical event)`. `contextforge audit verify` detects:
  - modified rows;
  - deleted rows (sequence gaps);
  - truncation, when you pass the exported head with `--expected-head`.

  All three are tested.
- **Limitation.** This is **tamper-evident, not tamper-proof.** Someone with write access to the file can rewrite the whole chain. Ship `audit export` and the head hash to a separate system if that matters.
- **Atomic with state changes.** State transitions write their audit event in the same transaction, so a decision cannot commit without its event.

## Phase 3 known limitations
- **No live verification.** Slack, GitHub and the model provider have not been verified live: no credentials were available. See `PHASE_3_HANDOFF.md`.
- **Socket Mode is the supported production path.** HTTP mode uses Bolt's built-in server. Run it only behind a TLS reverse proxy.
- **Lexical scrubbing.** The outbound secret scrubber is lexical. It is a last line of defence, not the primary control. The primary control is that no secret is ever available to the model or the MCP server.
- **Shared database.** The operational database is a single SQLite file. That is fine for a 5–10 user pilot; see `DEPLOYMENT.md` for when to move on.
