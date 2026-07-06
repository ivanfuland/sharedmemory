#!/usr/bin/env bash
# gbrain nightly backup — pg_dump (-Fc) of the pg-memory DB + rsync of the brain-repo, to NAS, with rotation.
#
# Replaces the misdirected infra/backup/backup-brain.sh (wrong DEST default, wrong brain-repo path,
# and dead-component cruft: canonical sqlite / distill bridge-state). Backs up exactly the two gbrain
# assets: the Postgres brain (pg-memory) and the brain-repo markdown mirror.
#
# No secrets live in this file (safe for the PUBLIC repo): pg_dump runs INSIDE the pg-memory container
# (superuser trust over the container socket), so no password is passed. Only paths appear here.
#
# The whole point of a backup is to be LOUD on trouble, never silently succeed with a bad/missing
# backup. Exit non-zero on any failure so the Inngest wrapper (gbrain-backup cron) can TG-alert.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="${GBRAIN_BACKUP_DEST:-$HOME/nas/openclaw/backups/gbrain}"
BRAIN_REPO="$ROOT/sandbox/gbrain-pg/brain-repo"
KEEP="${GBRAIN_BACKUP_KEEP:-7}"
CONTAINER="${GBRAIN_PG_CONTAINER:-pg-memory}"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOCKFILE="${TMPDIR:-/tmp}/gbrain-backup.lock"

# --- guard 0: single-flight. Two runs in the same second would write the same gbrain-<stamp>.dump and
#     rsync into the same brain-repo-<stamp>/, producing a corrupt/mixed backup. flock serialises them;
#     a second concurrent invocation cleanly skips (nothing to alert — the holder does the backup). ---
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "[backup] another backup run holds the lock; skipping this invocation"; exit 0
fi

# --- guard 1: KEEP must be a positive integer. A 0 / negative / non-numeric value makes the rotation
#     comparison error out on the LEFT of &&, which (under set -e) does NOT abort and falls through to
#     rm -rf for EVERY entry — deleting even the just-written backup. Fail loud instead. ---
if ! [[ "$KEEP" =~ ^[1-9][0-9]*$ ]]; then
  echo "[backup] FATAL: GBRAIN_BACKUP_KEEP must be a positive integer, got '$KEEP'"; exit 1
fi

# --- guard 2: if DEST is on the NAS, the share MUST actually be mounted. Otherwise mkdir -p silently
#     creates a LOCAL directory under the mountpoint and the backup lands on the local disk while the
#     cron sees exit 0 — i.e. you believe you have off-box nightly backups but you don't. ---
NAS_PREFIX="$HOME/nas/"
if [[ "$DEST" == "$NAS_PREFIX"* ]]; then
  rest="${DEST#"$NAS_PREFIX"}"; share="${rest%%/*}"; SHARE_ROOT="$NAS_PREFIX$share"
  # stat first to trigger any autofs mount, then require it to be a real mountpoint.
  ls "$SHARE_ROOT" >/dev/null 2>&1 || true
  if ! mountpoint -q "$SHARE_ROOT" 2>/dev/null; then
    echo "[backup] FATAL: NAS share not mounted at $SHARE_ROOT — refusing to back up to local disk"; exit 1
  fi
fi

mkdir -p "$DEST"
echo "[backup] dest=$DEST stamp=$STAMP keep=$KEEP"

# 1) Postgres custom-format dump (restore via `pg_restore`; -Fc matches the manual 2026-07-05 baseline).
DUMP="$DEST/gbrain-$STAMP.dump"
if ! docker exec "$CONTAINER" pg_dump -U gbrain -Fc gbrain > "$DUMP"; then
  echo "[backup] FATAL: pg_dump failed (is container '$CONTAINER' running?)"; rm -f "$DUMP"; exit 1
fi
# Integrity check, not just a smell test: pg_restore -l reads the archive TOC; a truncated or corrupt
# dump (incl. an interleaved partial write) fails here even if it starts with PGDMP and is large.
if ! docker exec -i "$CONTAINER" pg_restore -l < "$DUMP" > /dev/null 2>&1; then
  echo "[backup] FATAL: dump is not a readable pg_restore archive (truncated/corrupt); removing"; rm -f "$DUMP"; exit 1
fi
echo "[backup] pg dump  → $DUMP ($(( $(stat -c %s "$DUMP") / 1024 / 1024 )) MB, pg_restore -l OK)"

# 2) brain-repo markdown snapshot (timestamped mirror; --archive, never --delete).
if [ ! -d "$BRAIN_REPO" ]; then
  echo "[backup] FATAL: brain-repo not found at $BRAIN_REPO"; exit 1
fi
rsync -a "$BRAIN_REPO/" "$DEST/brain-repo-$STAMP/"
echo "[backup] brain-repo → $DEST/brain-repo-$STAMP/ ($(find "$DEST/brain-repo-$STAMP" -name '*.md' | wc -l) md)"

# 3) Rotation: keep only the $KEEP newest of each set (mtime desc). A delete failure is surfaced
#    (non-zero exit at the end) rather than swallowed by the && — the backup itself is already safe.
ROTATE_FAIL=0
prune() {  # $1 = filename prefix under $DEST
  local n=0 p
  while IFS= read -r p; do
    n=$((n + 1))
    [ "$n" -le "$KEEP" ] && continue
    if rm -rf "$p"; then
      echo "[backup] rotate rm $(basename "$p")"
    else
      echo "[backup] WARN: rotate rm failed: $p" >&2; ROTATE_FAIL=1
    fi
  done < <(ls -1dt "$DEST/$1"* 2>/dev/null || true)
}
prune "gbrain-"       # gbrain-<stamp>.dump
prune "brain-repo-"   # brain-repo-<stamp>/

if [ "$ROTATE_FAIL" = 1 ]; then
  echo "[backup] WARN: backup landed OK but rotation had delete failures — investigate (exiting non-zero to surface it)"
  exit 1
fi
echo "[backup] done → $DEST (dump + brain-repo, rotate keep $KEEP)"
