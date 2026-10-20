# Phase 3 handoff: Slack product, context-improvement loop, governance and deployment

Machine-readable summary: [`phase3_handoff.json`](phase3_handoff.json).

## Phase 0 re-verification (done first)
- **Docs read:** AGENTS.md and the Phase 1, Phase 2 and methodology documents.
- **Git history:** the repository has **no commits** (`master`, empty) and no remote.
- **Test baseline:** 253 passed and 1 skipped (live, BLOCKED) before any Phase 3 change.
- **MCP server:** exercised over real stdio (`tests/mcp`).
- **Agent:** executed end to end with the scripted test double. Slack and GitHub credentials are not set. **Update after Phase 3:** with the operator's `OPENAI_API_KEY`, the live agent test passed and a real `ask` matched gold `cf-017` (see IMPLEMENTATION_STATUS). The first live call exposed a real defect (Chat Completions rejects tools + reasoning for gpt-5.5); the adapter now uses the Responses API.

**Foundational defects fixed:**
1. **`agent/provenance.py` crashed when the warehouse file was missing.** This happened before the agent started, so a database outage looked like an internal error. Missing files are now recorded as `null`, and the governed query path reports `DATABASE_UNAVAILABLE` (test: `test_database_outage_*`).
2. **Phase 3 bugs found by the new tests.**
   - **Context version on tool-less turns:** a clarification turn that calls no tools has no `context_version`, which skipped the stale-context check. The served release version is now recorded.
   - **Missing lifecycle edge:** the `AWAITING_REVIEW → BLOCKED` edge was missing.

## Completion levels (kept separate on purpose)
| Feature | Implemented | Unit-tested | Integration-tested (real components) | Externally verified | Deployed | Used by real people |
|---|---|---|---|---|---|---|
| Slack app (Bolt, Socket Mode/HTTP, DM, mentions, threads, buttons, modals) | ✔ | ✔ signed-request + Web API contract | ✔ flows via `ProductCore` + real agent/MCP | ✘ no Slack credentials | ✘ | ✘ |
| Question → governed answer with SQL, definitions, certification, caveats, trace | ✔ | ✔ | ✔ (scripted model) | ✔ model only (CLI smoke); ✘ Slack | ✘ | ✘ |
| Clarification state machine (scoped, TTL, cancel, stale, edits/deletes) | ✔ | ✔ | ✔ | ✘ | ✘ | ✘ |
| Failure taxonomy A–Q + classifier | ✔ | ✔ 13 cases | ✔ | n/a | ✘ | ✘ |
| Improvement engine (evidence, human-decision routing, grounded LLM drafts) | ✔ | — | ✔ (scripted drafter, real schema and governed queries) | ✘ | ✘ | ✘ |
| Structured proposals, lifecycle, authorization | ✔ | ✔ incl. concurrency and replay | ✔ | n/a | ✘ | ✘ |
| Evaluation-driven validation (before/after × N, regression, violations) | ✔ | — | ✔ static + BLOCKED paths only | ✘ live runs need a model | ✘ | ✘ |
| GitHub integration (App/token, branch, commit, PR, reviewers, labels, comments, sync, checks, reviews) | ✔ | ✔ contract fake | ✔ workflow vs fake | ✘ no GitHub credentials, no remote | ✘ | ✘ |
| Human approval in Slack (answers + context changes) | ✔ | ✔ | ✔ | ✘ | ✘ | ✘ **0 approvals** |
| Durable audit (append-only, hash chain, backup) | ✔ | ✔ tamper detection | ✔ | n/a | ✘ | ✘ |
| Context releases (validate, atomic switch, verify, rollback, git-commit deploy) | ✔ | — | ✔ (git-commit path not exercised: no commits) | ✘ | local only | ✘ |
| Ops: /livez /readyz /metrics, pilot report, CSV export, retention | ✔ | — | ✔ | ✘ | ✘ | ✘ |
| Shadow mode | ✔ | — | ✔ | ✘ | ✘ | ✘ |
| Docker Compose deployment, backup script, Slack manifest | ✔ | — | ✘ Docker unavailable | ✘ | ✘ | ✘ |
| CI: secrets scan, dependency audit, proposal-scope, product-live workflow | ✔ | ✔ scope checker vs a real git repo | ✘ no GitHub Actions run | ✘ | — | — |

## Final end-to-end acceptance chain: where it stops
| Step | Status |
|---|---|
| 1. A permitted user asks in Slack | **STOPPED HERE: BLOCKED.** No Slack app or tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`) and no workspace or pilot users. |
| 2. A genuine Slack event is received | Blocked by step 1. Bolt routing is verified with signed synthetic requests only. |
| 3–5. Governed context via real MCP, SQL on real DuckDB, Slack shows result, SQL and definitions | Verified with real MCP and DuckDB and a **scripted model** plus a recording gateway. The live model works via CLI (smoke test); not yet through Slack. |
| 6–7. Insufficient-context question → abstain or clarify | Runtime verified (scripted). Live behaviour: only `tests/live` (email request abstains). |
| 8. The classifier identifies a genuine gap | Classifier verified on recorded responses; no genuine live failure exists. |
| 9. The engine drafts a grounded correction | Verified with a scripted drafter against the real schema and governed queries. Live drafting BLOCKED. |
| 10. A real PR | **BLOCKED:** no GitHub credentials, no remote repository, no commits. |
| 11–12. A real reviewer receives the request and decides | **BLOCKED:** no Slack and no human reviewers (owners are placeholders). |
| 13. The decision is persisted in the audit log | Verified with test decisions; **0 real decisions**. |
| 14. Evaluation against the proposed context | Implemented. Not run yet (needs a real proposal; model access now available). |
| 15–16. Merge when eligible; deploy | Eligibility gates verified against the fake. Deploy of a local release verified. Git-commit deploy unexercised (no commits). |
| 17–18. Rerun the failing scenario; preserve before/after evidence | Implemented in validation (`reproduction` + `regression` in `validation_json`). Not run live. |

## Exact actions needed from humans
1. ~~Provide `OPENAI_API_KEY`~~ done locally. Still needed: the same secret in the deployment and in the `live-eval` environment, and optionally `CONTEXTFORGE_PRICES_FILE`.
2. Create the Slack app from `deploy/slack/manifest.yaml`, install it, and supply `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`. Create the reviewer channel.
3. Commit the repository, push it to GitHub, create the GitHub App (permissions in `DEPLOYMENT.md`), and configure branch protection and the protected environments (`live-eval`, `product-live`).
4. Add real reviewers as `kind: human` owners (by PR), and fill in `config/product.yaml`: team id, users with labels, answer and context reviewers.
5. Provision a VM and run the `DEPLOYMENT.md` checklist.
6. Recruit 5–10 consenting users and run `PILOT_PROTOCOL.md`.
7. Run the Phase 2 experiment, then human review of gold labels (unchanged Phase 2 blockers).

## Two-minute demo script (`docs/DEMO_SCRIPT.md`)
Every step that needs Slack, GitHub or a live model is marked **NOT YET EXECUTABLE**.
