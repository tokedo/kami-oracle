#!/usr/bin/env bash
# Nightly DuckDB backup to GCS.
#
# Calls the loopback-only POST /backup endpoint so the running serve
# process can run EXPORT DATABASE under its own DB lock — file-level
# copies would race the poller writes.
#
# Cron entry (one line):
#   15 4 * * * /home/anatolyzaytsev/kami-oracle/scripts/backup-db.sh \
#       >> /home/anatolyzaytsev/kami-oracle/logs/backup.log 2>&1

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BUCKET="gs://kami-oracle-backups"
PROJECT="kami-agent-prod"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="$REPO/db/export-$STAMP"
TARBALL="$STAGE.tar.gz"

mkdir -p "$REPO/logs"
echo "[$(date -u +%FT%TZ)] backup: start dest=$STAGE"

TOKEN=$(grep '^KAMI_ORACLE_API_TOKEN=' "$REPO/.env" | cut -d= -f2-)
if [[ -z "$TOKEN" ]]; then
    echo "[$(date -u +%FT%TZ)] backup: ERROR no KAMI_ORACLE_API_TOKEN in .env" >&2
    exit 1
fi

# Hot export via the running serve process (loopback-only endpoint).
RESP=$(curl -sf -X POST http://127.0.0.1:8787/backup \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"dest_dir\":\"$STAGE\"}")
echo "[$(date -u +%FT%TZ)] backup: export ok resp=$RESP"

tar -C "$(dirname "$STAGE")" -czf "$TARBALL" "$(basename "$STAGE")"
echo "[$(date -u +%FT%TZ)] backup: tarball=$TARBALL size=$(du -h "$TARBALL" | cut -f1)"

gcloud storage cp "$TARBALL" "$BUCKET/" --project="$PROJECT"
echo "[$(date -u +%FT%TZ)] backup: uploaded to $BUCKET/$(basename "$TARBALL")"

rm -rf "$STAGE" "$TARBALL"

# Retention: keep last 14 backups in the bucket.
KEEP=14
ALL=$(gcloud storage ls "$BUCKET/" --project="$PROJECT" | grep -E '/export-.*\.tar\.gz$' | sort)
N=$(echo "$ALL" | wc -l)
if (( N > KEEP )); then
    DROP=$(echo "$ALL" | head -n $((N - KEEP)))
    echo "[$(date -u +%FT%TZ)] backup: pruning $((N - KEEP)) old object(s)"
    echo "$DROP" | xargs -r gcloud storage rm --project="$PROJECT"
fi

echo "[$(date -u +%FT%TZ)] backup: done"
