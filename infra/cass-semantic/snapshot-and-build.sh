#!/usr/bin/env bash
# CASS 语义栈：一致快照 → 全量建 bge-m3 索引 → 原子发布（幂等 + flock）
# 全程零碰 baseline 活 data_dir：只对 VACUUM INTO 快照副本操作（codex R1 P0#2）。
# 无真增量（fingerprint 变即全量重建，codex R1 P0#3）→ 这是周期性全量重建。
set -euo pipefail
LIVE="${LIVE_DATA_DIR:-$HOME/.local/share/coding-agent-search}"
SEM="${SEM_DATA_DIR:-$HOME/.local/share/cass-infinity-semantic}"   # fork 独占
URL="${CASS_INFINITY_URL:-http://127.0.0.1:7997}"
BIN="${CASS_BIN:-$HOME/.local/bin/cass-infinity}"
mkdir -p "$SEM/releases"
exec 9>"$SEM/.backfill.lock"; flock -n 9 || { echo "another build running"; exit 0; }   # 互斥
curl -sf -m5 "$URL/health" >/dev/null || { echo "FAIL: Infinity down"; exit 2; }
[ -r "$SEM/snapshot-cmd.sh" ] || { echo "FAIL: snapshot-cmd.sh 缺失——先跑 Phase 0"; exit 1; }   # codex R3 P2
TS=$(date +%Y%m%d-%H%M%S); STAGE="$SEM/releases/$TS"; mkdir -p "$STAGE"
trap 'rm -f "$SEM/current.tmp"' EXIT   # 防残留 current.tmp 让下次 ln 失败（codex R3 P1）

# 1) 事务一致快照（do_snapshot 首选 VACUUM INTO + quick_check；fallback=pause-reflink。
#    sqlite3 已实测能读 frankensqlite。不跑慢且 schema 不定的 whole-doctor（codex R3 P0）。
source "$SEM/snapshot-cmd.sh"
do_snapshot "$LIVE/agent_search.db" "$STAGE/agent_search.db" || { echo "FAIL: snapshot(do_snapshot 非0)"; rm -rf "$STAGE"; exit 1; }
export CASS_DATA_DIR="$STAGE" CASS_INFINITY_URL="$URL"
# 轻量 sanity（一致性已由 do_snapshot 保证；此处只防空/不可读）
"$BIN" status --json 2>/dev/null | python3 -c "import sys,json;db=json.load(sys.stdin).get('database',{});sys.exit(0 if db.get('opened') and not db.get('open_error') else 1)" \
  || { echo "FAIL: 快照不可打开"; rm -rf "$STAGE"; exit 1; }

# 2) 词法索引：--force-rebuild（从 canonical DB 只读重建 Tantivy，不扫文件系统——codex R3 P0）
( set -o pipefail; "$BIN" index --force-rebuild 2>&1 | tail -3 ) || { echo "FAIL: lexical force-rebuild"; rm -rf "$STAGE"; exit 1; }
# 非空守卫（term-independent 目录大小阈值，codex R5 P2；实测全量词法索引 ~185M）
[ -d "$STAGE/index" ] || { echo "FAIL: lexical index missing"; rm -rf "$STAGE"; exit 1; }
idxk=$(du -sk "$STAGE/index" 2>/dev/null | cut -f1); [ "${idxk:-0}" -gt 500 ] \
  || { echo "FAIL: 词法索引仅 ${idxk}KB（空快照/FS-scan 空路径？预期 MB 级）"; rm -rf "$STAGE"; exit 1; }

# 3) bge-m3 全量语义索引（循环至 published）—— 写打开的是快照副本，活 DB 零接触
prev=-1; pub=""
for i in $(seq 1 80); do
  out=$("$BIN" models backfill --tier quality --embedder infinity --scheduled --batch-conversations 999999 --json 2>>/tmp/cc-snap-build.err) \
    || { echo "FAIL: backfill errored"; rm -rf "$STAGE"; exit 1; }
  read -r pub off < <(printf '%s' "$out" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('published'),d.get('last_offset'))") \
    || { echo "FAIL: parse"; printf '%s' "$out" | head -c 500; rm -rf "$STAGE"; exit 1; }
  echo "[build iter $i] published=$pub offset=$off"
  [ "$pub" = "True" ] && break
  [ "$off" = "$prev" ] && { echo "STALLED at $off"; rm -rf "$STAGE"; exit 1; }
  prev=$off
done
[ "$pub" = "True" ] || { echo "FAIL: bge-m3 not published"; rm -rf "$STAGE"; exit 1; }
# publish gate：词法 + bge-m3 都在才算完整 serving dir（codex R2 P0）
[ -d "$STAGE/index" ] && [ -f "$STAGE/vector_index/index-bge-m3.fsvi" ] || { echo "FAIL: serving dir 不完整"; rm -rf "$STAGE"; exit 1; }

# 4) 原子发布：symlink mv -Tf（codex R2 P0 无空窗；codex R3 P1 先 rm 残留 tmp）
rm -f "$SEM/current.tmp"
ln -s "releases/$TS" "$SEM/current.tmp"
mv -Tf "$SEM/current.tmp" "$SEM/current"     # rename 替换 symlink 本身，同目录同 FS 原子无空窗
trap - EXIT                                   # 发布成功，解除 tmp 清理 trap
ls -dt "$SEM"/releases/*/ 2>/dev/null | tail -n +3 | xargs -r rm -rf   # 留最近 2 份给在跑 search 的 mmap 退场
echo "OK: published -> $SEM/current -> releases/$TS"
