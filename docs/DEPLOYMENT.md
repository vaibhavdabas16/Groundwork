# Deployment (small real-user pilot, single VM)

> **Status: implemented, NOT verified.** Docker was not available on the authoring machine. No
> Slack app, GitHub App or VM existed during development. Work through the checklist at the end
> before calling the deployment verified.

## Prerequisites
- **VM:** Linux with 2 vCPU, 4 GB RAM and 20 GB disk; Docker Engine plus the Compose plugin; outbound HTTPS to `slack.com`, the model provider and `api.github.com`. No inbound ports are needed in Socket Mode.
- **Slack:** a workspace where you may install apps.
- **GitHub:** a repository holding this project. Prefer a GitHub App installed only on that repository.
- **Model:** an API key, OpenAI or Anthropic.

## 1. Slack app
1. Go to https://api.slack.com/apps → **Create New App** → **From an app manifest**, and paste `deploy/slack/manifest.yaml`.
2. **Bot scopes:** only `app_mentions:read`, `chat:write` and `im:history`. **Events:** `app_mention`, `message.im`. Interactivity and Socket Mode are on.
3. **Basic Information → App-Level Tokens:** create a token with `connections:write`. That is `SLACK_APP_TOKEN` (`xapp-…`).
4. **Install to Workspace.** Copy the Bot User OAuth Token: that is `SLACK_BOT_TOKEN` (`xoxb-…`).
5. Create a **private** reviewer channel and invite the bot (`/invite @groundwork`). Copy the channel ID.
6. Find the workspace team ID (`T…`) and the member IDs (`U…`) of your pilot users and reviewers.
7. **HTTP mode (optional):** also set `SLACK_SIGNING_SECRET`, run `product run --http 3000` behind a TLS reverse proxy, and set the Request URL to `https://<host>/slack/events`.

## 2. GitHub App (recommended) or a fine-grained token
- **App permissions** (repository):
  - Contents: read & write;
  - Pull requests: read & write;
  - Issues: read & write (labels and comments);
  - Checks: read;
  - Commit statuses: read;
  - Metadata: read.
- **Install it on this repository only.** Set:
  - `GITHUB_APP_ID`;
  - `GITHUB_APP_INSTALLATION_ID`;
  - `GITHUB_APP_PRIVATE_KEY_PATH`, the PEM mounted as a file or Docker secret.
- **Fine-grained token alternative:** scope it to the same repository with the same permissions and set `GROUNDWORK_GITHUB_TOKEN`.
- **Branch protection on `main`** must be configured by a repository admin; it is **not** configured by this project:
  - require a pull request with at least one approving review;
  - dismiss stale approvals;
  - required status checks: `ci / quality`, `ci / integration`, `ci / secrets-scan`, `ci / dependency-audit`, `ci / proposal-scope`, `live-eval / live-eval`;
  - include administrators;
  - forbid force pushes.
- **Environments:** create the protected environments `live-eval` and `product-live` with required reviewers, and store their secrets there.

## 3. Configure
```bash
cp deploy/env.example deploy/.env && chmod 600 deploy/.env      # fill in the tokens
cp config/product.example.yaml config/product.yaml
```
In `config/product.yaml`, set:
- `slack.allowed_team_ids`;
- `review_channel_id`;
- `users` (pilot allow-list with labels);
- `answer_reviewers`;
- `context_reviewers`;
- `github.repository`.

Every reviewer `owner_id` must be a `kind: human`, `placeholder: false` entry in `context/ownership/owners.yaml`, added through a reviewed PR. Startup refuses placeholder or agent reviewers.

## 4. Build data, deploy a context release, start
```bash
docker compose --profile setup run --rm setup                   # acquire + build + quality checks
docker compose run --rm app context deploy --path /app/context --actor "<your name>"
docker compose run --rm app product check                       # config, credentials present, readiness
docker compose up -d app
curl -fsS http://127.0.0.1:8088/readyz                           # 200 only if DB, warehouse, release, model key are OK
```
`product run` refuses to start without a deployed release, unless you pass `--allow-working-tree`, which is for development only.

## 5. Operations
| Concern | Implementation |
|---|---|
| Liveness / readiness | `/livez`, `/readyz` (503 with failing checks), compose healthcheck |
| Metrics | `/metrics`, Prometheus text, aggregates only |
| Logs | JSON to stderr; Docker `json-file` rotation at 20 MB × 5 |
| Restart | `restart: unless-stopped`; on start, in-flight requests are marked INTERRUPTED and users are told |
| Graceful shutdown | SIGTERM drains the worker pool; `stop_grace_period: 120s` |
| Resource limits | `mem_limit: 3g`, `cpus: 2`, `pids_limit: 256`; per-user hourly cap; query limits from the policy |
| Least privilege | Non-root (uid 10001), read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, warehouse volume mounted **read-only** |
| Secrets | `deploy/.env` (600) or Docker secrets; never in images, config or logs |
| Backups | `deploy/backup.sh` (cron, nightly): SQLite online backup with audit-chain verification, plus an audit JSONL export and a copy of the context releases |
| Migrations | Forward-only numbered migrations applied at start (`opsdb migrate`); back up before upgrading |
| Retention | `retention_days` (default 90): question and answer text purged by the periodic job; audit events keep ids and states |

## 6. Recovery
- **Restore the operational DB:**
  1. Stop the app.
  2. Copy `ops-<stamp>.sqlite` to the `cf-ops` volume as `product.sqlite`.
  3. Run `groundwork audit verify --expected-head <head from backup-<stamp>.json>`.
  4. Start the app.
- **Bad context release:** `groundwork context rollback --actor <name>` (re-validates the target).
- **Lost warehouse:** re-run the `setup` profile. Checksums are verified against `data/manifests`.

## Verification checklist (none of this has been done yet)
- [ ] `docker compose build` succeeds; the container runs as uid 10001; the root filesystem is read-only.
- [ ] `docker compose run --rm app product check`: config ok, credentials present.
- [ ] `/readyz` returns 200 after the release deploy.
- [ ] A DM to the bot from an allow-listed user gets the consent notice, then an answer (shadow: a review card).
- [ ] A reviewer approves the answer; the user receives it.
- [ ] `uv run pytest -m live tests/live` passes in the `product-live` environment.
- [ ] `deploy/backup.sh` produces a backup whose audit chain verifies.
