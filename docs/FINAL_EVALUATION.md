# Final evaluation

## Status: BLOCKED — no live evaluation results exist

| Evaluation | State | Why |
|---|---|---|
| Phase 2 three-mode experiment (RAW vs DESCRIPTIONS vs FULL, held-out, 3 repetitions) | **Not run** | Started as `run-20261121T100535-4bc95e`; **stopped by the operator at 24/630 attempts** (OpenAI budget limit). Marked `INCOMPLETE`; not a result. A full run needs about $40–50 of credit. |
| Context-fix before/after measurements | **None** | No real failures, proposals or validations yet |
| Real context proposals generated from user traffic | **0** | No users |
| Human approval records | **0** | No reviewers configured |
| PRs created / merged | **0 / 0** | No GitHub credentials; the repository has no commits and no remote |
| Real-user activity | **None** | See `PILOT_RESULTS.md` |

## What has been verified (machinery, not quality)
Latest full run, with `OPENAI_API_KEY` set. Total: 341 passed, 3 skipped (live Slack and GitHub BLOCKED; one readiness test that only applies when no credentials are present). The live OpenAI test passed.

| Suite | Result |
|---|---|
| `tests/unit` | 187 passed |
| `tests/agent tests/mcp tests/live` | 49 passed, 3 skipped as BLOCKED |
| `tests/integration tests/security` | 105 passed |

Other checks:
- ruff, format, mypy (strict) and `validate-context`: clean.
- `eval gold check`: 81/81 gold queries re-execute identically.
- `eval gold verify`: the held-out lock is intact.

The Phase 3 tests use the **real** MCP server, SQL guard and warehouse. Slack and GitHub are represented by the real Bolt and `slack_sdk` libraries against local mocks, and by an httpx contract fake. The model is a labelled scripted test double. None of these results says anything about answer quality.

## How the closed loop is measured once credentials exist
1. **Baseline.** Run the Phase 2 full experiment: `eval run --all --split heldout --repetitions 3`.
2. **Per proposal.** `proposals validate --id <id> --repetitions 3` runs on the current and the staged context with the same snapshot, model config, prompt hash, scoring and tools:
   - the original question(s), 3 times each;
   - the eval cases whose refs touch the affected objects, plus 6 sentinel dev cases.

   Per-case majority correctness is compared. Any regression or violation means `VALIDATION_FAILED`.
3. **After deployment.** Re-run the experiment on the new release and compare with `eval compare`.
4. **Reporting.** Report paired, question-level differences with bootstrap CIs (Phase 2 methodology). A single correct response is never called an improvement.
