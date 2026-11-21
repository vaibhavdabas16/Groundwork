# Pilot results

## Status: Pilot prepared; no verified user activity yet.

No real user has used Groundwork. These numbers do not exist yet:
- pilot users;
- questions;
- reviewer decisions;
- feedback;
- usage.

This file contains no invented numbers.

| Prerequisite | State |
|---|---|
| Slack app installed in a workspace | **Not done.** No Slack credentials were available. |
| Pilot users recruited and consented | **Not done.** Requires real people. |
| Reviewers registered in `owners.yaml` / `config/product.yaml` | **Not done.** Only placeholders exist. |
| Model credentials (`OPENAI_API_KEY`) | **Not set** in the development environment. |
| Deployment on a VM | **Not done.** |

`groundwork pilot report` on the development database returns `"status": "Pilot prepared; no verified user activity yet."` with `questions: 0`. That state is asserted by `tests/integration/test_product_ops.py`.

## How to fill this in
After day 7 of `PILOT_PROTOCOL.md`:
1. Paste the output of `groundwork pilot report` here, plus a link to the exported CSV and audit JSONL.
2. Give the deployment id and the context release version(s) used.
3. Give the model id.
4. Give the dates.

Report only what those files contain.
