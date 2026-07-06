#!/usr/bin/env bash
# gbrain nightly backup — pg_dump (-Fc) of the pg-memory DB + rsync of the brain-repo, to NAS, with rotation.
#
# Replaces the misdirected infra/backup/backup-brain.sh (wrong DEST default, wrong brain-repo path,
# and dead-component cruft: canonical sqlite / distill bridge-state). This script backs up exactly the
# two gbrain assets: the Postgres brain (pg-memory) and the brain-repo markdown mirror.
#
# No secrets live in this file (safe for the PUBLIC repo): pg_dump runs INSIDE the pg-memory container
# (superuser trust over the container socket), so no password is passed. Only paths appear here.
#
# Exit non-zero on any failure so the Inngest wrapper (gbrain-backup cron) can TG-alert.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="${GBRAIN_BACKUP_DEST:-$HOME/nas/openclaw/backups/gbrain}"
BRAIN_REPO="$ROOT/sandbox/gbrain-pg/brain-repo"
KEEP="${GBRAIN_BACKUP_KEEP:-7}"
CONTAINER="${GBRAIN_PG_CONTAINER:-pg-memory}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$DEST"
echo "[backup] dest=$DEST stamp=$STAMP keep=$KEEP"

# 1) Postgres custom-format dump (restore via `pg_restore`; -Fc matches the manual 2026-07-05 baseline).
DUMP="$DEST/gbrain-$STAMP.dump"
if ! docker exec "$CONTAINER" pg_dump -U gbrain -Fc gbrain > "$DUMP"; then
  echo "[backup] FATAL: pg_dump failed (is container '$CONTAINER' running?)"; rm -f "$DUMP"; exit 1
fi
# Sanity: a valid custom-format archive starts with the "PGDMP" magic and is non-trivially sized.
if [ "$(head -c 5 "$DUMP")" != "PGDMP" ] || [ "$(stat -c %s "$DUMP")" -lt 1000000 ]; then
  echo "[backup] FATAL: dump failed sanity check (magic/size); removing partial"; rm -f "$DUMP"; exit 1
fi
echo "[backup] pg dump  → $DUMP ($(( $(stat -c %s "$DUMP") / 1024 / 1024 )) MB)"

# 2) brain-repo markdown snapshot (timestamped mirror; --archive, never --delete).
if [ ! -d "$BRAIN_REPO" ]; then
  echo "[backup] FATAL: brain-repo not found at $BRAIN_REPO"; exit 1
fi
rsync -a "$BRAIN_REPO/" "$DEST/brain-repo-$STAMP/"
echo "[backup] brain-repo → $DEST/brain-repo-$STAMP/ ($(find "$DEST/brain-repo-$STAMP" -name '*.md' | wc -l) md)"

# 3) Rotation: keep only the $KEEP newest of each set (sorted by mtime desc), delete the rest.
prune() {  # $1 = filename prefix under $DEST
  local n=0 p
  while IFS= read -r p; do
    n=$((n + 1))
    [ "$n" -le "$KEEP" ] && continue
    rm -rf "$p" && echo "[backup] rotate rm $(basename "$p")"
  done < <(ls -1dt "$DEST/$1"* 2>/dev/null || true)
}
prune "gbrain-"       # gbrain-<stamp>.dump
prune "brain-repo-"   # brain-repo-<stamp>/

echo "[backup] done → $DEST (dump + brain-repo, rotate keep $KEEP)"
