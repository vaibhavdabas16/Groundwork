# Groundwork architecture

```
                    Slack workspace (Socket Mode WebSocket, outbound only)
                                   │ app_mention / message.im / block_actions / view_submission
┌──────────────────────────────────▼───────────────────────────────────────────────────────────┐
│ app container: `groundwork product run`                                                      │
│                                                                                                │
│  slack_app.py (Bolt) ── ack ≤3 s ──► core.ProductCore                                          │
│     dedupe(event_id) · allow-list · consent · rate limit · conversations (bounded state machine) │
│     │ worker pool (max_concurrent_requests)                                                    │
│     ▼                                                                                          │
│  agent.DataAgent ──► model provider (OpenAI / Anthropic; explicit, no fallback)                │
│     │ stdio (one MCP server per request; principal + context release fixed at start)          │
│     ▼                                                                                          │
│  mcp_server ─► GroundworkService ─► policy ─► SQL guard (AST) ─► read-only DuckDB worker     │
│                                                            (subprocess, killable)              │
│  failures.classify ─► FailureStore ─► proposals.engine (diagnose → draft, grounded)            │
│         │                                   │                                                  │
│         │ not context-fixable: stop          ▼                                                  │
│         │                      proposals.validation (static + before/after reruns + regression)│
│         │                                   ▼                                                  │
│         │                      proposals.workflow ──► GitHub (branch, commit, PR)              │
│         │                                   │        ◄── branch protection + required checks   │
│         ▼                                   ▼                                                  │
│  shadow review cards / proposal review cards in the reviewer channel (Approve / Reject / ...)   │
│  deployment.ContextDeployer: validated immutable releases + atomic CURRENT pointer + rollback  │
│  audit.AuditLog (append-only, hash chain) · ops.py (/livez /readyz /metrics on 127.0.0.1)      │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
   volumes: warehouse (read-only) · ops (product.sqlite, traces, MCP audit) · context_releases · evals
```

## Components and boundaries

| Module | Responsibility | Never does |
|---|---|---|
| `product/slack_app.py` | Bolt binding: Socket Mode or signed HTTP, ack, loop filtering, Web API with retry handlers | business decisions |
| `product/core.py` | Requests, identity mapping, consent, conversation state, delivery, shadow review, interaction handling, recovery | open the warehouse; decide context changes |
| `agent/` (Phase 2) | LLM tool loop through MCP only | read gold labels; access the DB directly |
| `mcp_server`, `service`, `security`, `query` (Phase 1) | The only data path | accept identity from arguments |
| `product/failures.py` | Deterministic failure taxonomy (A–Q) from recorded evidence | ask an LLM whether something is a context problem |
| `product/proposals/engine.py` | Evidence gathering; human-decision requests; LLM drafting with read-only tools; grounding check | write files outside staging; invent policy |
| `product/proposals/patches.py` | Typed operations → staged copy, path allow-list, schema checks, draft enforcement, diffs | touch protected paths |
| `product/proposals/validation.py` | Static checks, reproduction (N repetitions on both contexts), regression via the Phase 2 harness | promote without evidence; run with a test double |
| `product/proposals/store.py` | Lifecycle state machine, authorization of decisions, audit-in-transaction | let anyone but an authorized human reach APPROVED |
| `product/proposals/workflow.py` | PR creation and sync, merge eligibility, deploy of merged commits | merge by default; deploy unmerged content |
| `product/github.py` | REST client (App installation token or fine-grained token), idempotent ops | commit protected paths; merge unless enabled |
| `product/deployment.py` | Release validation, immutable copy, atomic switch, verify, rollback, git-commit deploys | deploy invalid or unmerged context |
| `product/audit.py`, `product/db.py` | Append-only hash-chained audit; migrations | UPDATE/DELETE audit rows (triggers) |
| `product/ops.py` | Readiness, metrics, pilot report, retention purge, CSV export | fabricate a metric (unknown → `null`) |

## Lifecycles

**Request:** `QUEUED → RUNNING → DONE | FAILED | CANCELLED | INTERRUPTED`. Restart marks in-flight requests INTERRUPTED and tells the user; nothing is re-executed silently.

**Conversation:** `AWAITING_CLARIFICATION → RESOLVED | EXPIRED | CANCELLED | STALE`.

**Answer review (shadow):** `PENDING → APPROVED | REJECTED | CORRECTED | CLARIFICATION_REQUESTED`.

**Context proposal:** `DETECTED → DIAGNOSED → PROPOSED → VALIDATING → AWAITING_REVIEW → APPROVED | REJECTED | CHANGES_REQUESTED → MERGE_ELIGIBLE → MERGED → DEPLOYED`.
- Side states: `BLOCKED`, `SUPERSEDED`, `VALIDATION_FAILED`.
- The edges are in `proposals/models.py: TRANSITIONS`.

Answer approval and context-change approval are separate tables, actions and permissions.

## Why this shape
- **Process model.** One container and one process, with a subprocess per question for MCP and the query worker. This keeps the Phase 1 isolation, needs no networked MCP (remote MCP stays disabled until OAuth exists), and runs on one small VM.
- **Database.** SQLite in WAL mode with IMMEDIATE transactions gives serialised decisions without a separate database server.

**When to evolve (not needed for a 5–10 user pilot):**
- **Postgres** for the operational DB, when there are more than about 50 concurrent users, several app replicas, or audit export to a WORM store is required.
- **A long-lived MCP service with authenticated transport,** when per-question subprocess start-up (about 1–2 s) dominates latency.
- **A queue (e.g. Redis or SQS),** only when several workers must share load.
