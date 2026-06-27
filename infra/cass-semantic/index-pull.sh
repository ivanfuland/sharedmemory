#!/usr/bin/env bash
# CASS 增量拉取 entrypoint（Inngest cass-index-daily 调用，镜像 distill/run-bridge.sh）。
# 词法 index 增量 + bge-m3 backfill 循环——**不用整合 `index --semantic`**（撞 upstream stall-abort
# #244/#258：语义阶段不推进 current 计数→watchdog exit 70；积压增量必现，codex P0#1）。
# 失败回滚 scan watermark：fork mid-batch 每 10s 存 scan_start_ts 到 last_scan_ts（mod.rs:12706，
# OOM-loop 规避），若词法摄入被杀于水位推进后/落库前，下轮 delta 扫描会跳过未落库文件→静默漏会话
# （codex P0#2）。本脚本词法失败时回滚水位；SIGKILL 无解但拆分后词法极快、窗口极小。
set -euo pipefail
CANON="${CASS_DATA_DIR:-$HOME/.local/share/coding-agent-search}"
URL="${CASS_INFINITY_URL:-http://127.0.0.1:7997}"
BIN="${CASS_BIN:-$HOME/.local/bin/cass-infinity}"
LOCK="$HOME/.local/share/.cass-write.lock"   # 全局写锁，与 full-reingest.sh 共用（codex P1#1）
DB="$CANON/agent_search.db"

emit_fail() { printf '{"ok":false,"error":"%s"}\n' "$1"; exit "${2:-1}"; }

exec 9>"$LOCK"; flock -n 9 || { echo '{"ok":false,"skipped":"another cass write holds lock"}'; exit 0; }
curl -sf -m5 "$URL/health" >/dev/null || emit_fail "Infinity down" 2
[ -d "$CANON" ] || emit_fail "canonical missing: $CANON"

# 快照 scan watermark；trap 在词法未完成（清退或 SIGTERM 超时）时回滚（SIGKILL 无法 trap）。
WM=$(sqlite3 "$DB" "SELECT key||char(9)||value FROM meta WHERE key LIKE 'last_scan_ts:%';" 2>/dev/null || echo "")
lexical_ok=0
restore_wm() {
  [ "$lexical_ok" = 1 ] && return 0    # 词法已成功→水位正确，不回滚（文件已落库）
  [ -n "$WM" ] || return 0
  printf '%s\n' "$WM" | while IFS=$'\t' read -r k v; do
    [ -n "$k" ] && sqlite3 "$DB" "UPDATE meta SET value='$(printf '%s' "$v" | sed "s/'/''/g")' WHERE key='$k';" 2>/dev/null
  done
}
trap restore_wm EXIT

# 1) 词法 + DB 增量 index（不带 --semantic）。失败→trap 回滚水位。
CASS_DATA_DIR="$CANON" CASS_INFINITY_URL="$URL" "$BIN" index >/tmp/cc-cass-pull.log 2>&1 \
  || emit_fail "lexical index failed (watermark rolled back)"
lexical_ok=1   # 词法成功，文件已落库，水位正确——往后失败不回滚（backfill 自带 checkpoint 续跑）

# 2) bge-m3 语义。先判语义是否已与 DB 一致（manifest fp 的 conv/msg == DB）——一致则跳过，
#    避免无新内容时每日 backfill 空转重 walk 整语料（~12min CPU；codex 之外的效率加固）。
pub=""
sem_current=$(python3 -c "
import json,re,sqlite3
try:
    m=json.load(open('$CANON/vector_index/semantic_manifest.json'))['quality_tier']
    mm=re.match(r'content-v\d+:(\d+):\d+:(\d+)', m.get('db_fingerprint',''))
    c=sqlite3.connect('$DB'); nc=c.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
    nm=c.execute('SELECT COUNT(*) FROM messages').fetchone()[0]; c.close()
    print('yes' if (m.get('ready') and mm and (int(mm.group(1)),int(mm.group(2)))==(nc,nm)) else 'no')
except Exception: print('no')
" 2>/dev/null || echo no)
if [ "$sem_current" = yes ]; then
  echo "semantic 已与 DB 一致，跳过 backfill"; pub=True
else
  prev=-1
  for i in $(seq 1 200); do
    out=$(CASS_DATA_DIR="$CANON" CASS_INFINITY_URL="$URL" "$BIN" models backfill --tier quality --embedder infinity --scheduled --batch-conversations 999999 --json 2>>/tmp/cc-cass-pull.log) \
      || emit_fail "backfill errored"
    read -r pub off < <(printf '%s' "$out" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('published'),d.get('last_offset'))" 2>/dev/null) \
      || emit_fail "backfill parse"
    [ "$pub" = "True" ] && break
    [ "$off" = "$prev" ] && emit_fail "backfill stalled at offset $off"
    prev=$off
  done
  [ "$pub" = "True" ] || emit_fail "bge-m3 not published"
fi

# 3) 末行 JSON 报告（conv/msg 计数供观测增量进展）
conv=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM conversations' 2>/dev/null || echo -1)
msg=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM messages' 2>/dev/null || echo -1)
ready=$(python3 -c "import json;print(str(json.load(open('$CANON/vector_index/semantic_manifest.json'))['quality_tier'].get('ready')).lower())" 2>/dev/null || echo unknown)
printf '{"ok":true,"conversations":%s,"messages":%s,"semantic_ready":"%s"}\n' "$conv" "$msg" "$ready"
