# Runbook

| Symptom | Check | Action |
|---|---|---|
| `/readyz` 503 `context_release: not ready` | `groundwork context status` | `context deploy --path <validated dir> --actor <you>`, or `context rollback` |
| `/readyz` 503 `model_credentials: false` | is `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` in `deploy/.env`? | Fix the env and restart. Requests meanwhile return an honest BLOCKED message. |
| Users get "model service unavailable" | `groundwork failures` shows `MODEL_UNAVAILABLE`; check `error.code` (`provider_rate_limited`, `provider_auth`) | Rate limit: wait, or lower `max_concurrent_requests`. Auth: rotate the key. **Never switch the model silently**; changing the model is a config change. |
| "warehouse is unavailable" | `failures` shows `DATABASE_UNAVAILABLE`; is the `cf-warehouse` volume mounted? | Re-run `setup`, or restore the volume. No context proposal is created for outages. |
| Answers not arriving | audit `slack.delivery_failed` (`groundwork audit export --out x.jsonl`) | Slack token or scopes. The periodic job redelivers finished live answers idempotently. |
| Review cards missing | is `review_channel_id` set, and is the bot in the channel? | Invite the bot. A pending review is listed in `answer_reviews`. |
| Proposal stuck `BLOCKED` | `groundwork proposals show --id <id>`, field `validation_json.blocked` | Usually missing model credentials, or a human business decision is needed. Fix, then `proposals validate --id <id>`. |
| Proposal `VALIDATION_FAILED` | `validation_json.regression.regressed_cases` | A human revises the proposal. Never weaken eval cases. |
| PR merged but not deployed | `proposals sync --id`, then `proposals deploy --id <id> --actor <you>` | Deploys from the merge commit on `origin/main` only. |
| PR merged outside the workflow → `BLOCKED` | Audit `proposal.transition` with reason | Review it manually. Deploy via `context deploy --git-commit <sha>` only after a human verifies the change. |
| Audit chain broken | `groundwork audit verify` | Treat as an incident: restore the last verified backup and compare. |
| Too many requests from one user | `per_user_requests_per_hour` | Adjust the config. |

**Routine:**
- **Daily:** `pilot report`.
- **Nightly:** `deploy/backup.sh`.
- **Weekly:** `live-eval` (full experiment).
- **Before upgrading:** take a backup and read the migration notes.
