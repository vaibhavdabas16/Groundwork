# Experiment results

## Status: NOT RUN — no complete live results exist

**No live model evaluation has been run.**

There are no accuracy figures, no context-improvement estimates, no confidence intervals, and no live latency or cost figures.

Nothing in this repository should be read as such a result. This file deliberately contains no invented numbers.

### Why

| Blocker | Detail |
|---|---|
| Budget (was: no provider credentials) | `OPENAI_API_KEY` was provided after Phase 3 and the live path passed a smoke test. The full experiment (630 attempts) was started as `run-20261121T100535-4bc95e` and **stopped by the operator after 24/630 attempts** because of an OpenAI budget limit (about $0.07 per attempt; the full run needs about $40–50). That run is marked `INCOMPLETE` and is not a result: 24 attempts cannot compare the modes. No fallback to implicit credentials or another provider exists. |
| Operator instruction | The person running this work said not to use any API of their account. No account-linked credential or connector was probed or used. |
| Full-experiment command not invoked | Attempting `eval run --all --split heldout --repetitions 3` was blocked by the environment's permission policy. That is consistent with the instruction above, so it was not retried. |
| Gold not human-reviewed | 0 of 70 answerable cases are human-reviewed. Even with credentials, results would be reported as being against SQL-validated, AI-authored gold. The regression gate stays BLOCKED until at least 50 held-out cases are reviewed. |

### What is missing

| Result | Requires |
|---|---|
| Per-mode metrics A–O, live, on the held-out split | the full 810-attempt experiment |
| RAW vs DESCRIPTIONS vs FULL paired differences, CIs, McNemar/Holm | the full experiment |
| Per-category differences, cost/latency trade-offs, consistency across 3 repetitions | the full experiment |
| Baseline for the regression gate | a COMPLETE live run on human-reviewed gold |

The full experiment is 70 held-out questions × 3 modes × 3 repetitions = 630 attempts. Adding the 20 dev questions gives 810 attempts.

## How to produce the results

```bash
export GROUNDWORK_PROVIDER=openai
export OPENAI_API_KEY=...               # in a shell or CI environment you control
export GROUNDWORK_PRICES_FILE=prices.json   # verified OpenAI prices; required for --budget-usd (format: agent/pricing.py)
uv run groundwork eval gold verify    # held-out lock intact
uv run groundwork eval run --mode full_context --split dev --repetitions 1 --budget-usd 5   # smoke
uv run groundwork eval run --all --split heldout --repetitions 3 --concurrency 3 \
    --budget-usd <cap> --kind experiment --label "phase2-full"
uv run groundwork eval report --run-id <run_id>     # data/evals/runs/<run_id>/{report.md,summary.json,attempts.csv}
uv run groundwork eval gate --run-id <run_id>
```

Run the dev split first, and do any prompt or agent iteration on dev only. Do not tune against held-out results.

There is no measured cost. Token usage and cost are recorded per attempt, and the `--budget-usd` cap makes the run INCOMPLETE rather than PASS when it is reached.

Paste the generated `report.md` here, or link it, together with:
- the run id;
- the requested and resolved model ids;
- the `snapshot_id`;
- the context version;
- the prompt hash.

## What WAS executed (non-live evidence)

These checks validate the machinery. They do **not** measure model or context quality.

| Check | Result |
|---|---|
| Real agent runtime against the real MCP server and governed SQL, with a scripted test double as the model | 14 robustness tests pass: timeouts, rate limits, loops, malformed calls, injection, cancellation, context source unavailable, etc. |
| Harness plumbing run: 4 cases × 3 modes × 2 repetitions, test double | COMPLETE. 24 attempts were scored against frozen gold. The report carries the "NOT A MODEL EVALUATION" banner. |
| Anthropic adapter against the real SDK with an HTTP mock transport | 11 contract tests pass |
| OpenAI adapter against the real SDK (3.57.0) with an HTTP mock transport, and through the real runtime + MCP server | 18 contract tests + 1 runtime wire test pass |
| Context-mode isolation | No leakage across exhaustive probes. Interfaces are identical over real stdio. |
| Gold SQL | 81 queries (70 answerable primaries + 1 answer-check value query + 10 alternative interpretations) execute through the governed path and re-check identically (`eval gold check`) |
