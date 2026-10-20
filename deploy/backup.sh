#!/usr/bin/env sh
# Nightly backup of operational metadata (run from the host's cron). Uses the SQLite online backup
# API (consistent while the app runs) and verifies the audit hash chain of the copy.
set -eu
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST=${BACKUP_DIR:-./backups}
mkdir -p "$DEST"
docker compose exec -T app contextforge opsdb backup --out "/app/data/ops/backup-$STAMP.sqlite" > "$DEST/backup-$STAMP.json"
docker compose cp "app:/app/data/ops/backup-$STAMP.sqlite" "$DEST/ops-$STAMP.sqlite"
docker compose exec -T app sh -c "rm -f /app/data/ops/backup-$STAMP.sqlite"
docker compose exec -T app contextforge audit export --out "/tmp/audit-$STAMP.jsonl" >/dev/null
docker compose cp "app:/tmp/audit-$STAMP.jsonl" "$DEST/audit-$STAMP.jsonl"
docker compose cp "app:/app/data/context_releases" "$DEST/context_releases-$STAMP"
find "$DEST" -mindepth 1 -maxdepth 1 -mtime +${RETENTION_DAYS:-30} -exec rm -rf {} +
echo "backup ok: $DEST/ops-$STAMP.sqlite"
