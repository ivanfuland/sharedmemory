#!/usr/bin/env bash
# CASS 单版本全新重摄入：备份老库 → fork 全量 index(JSONL→DB+词法+bge-m3 语义) 进临时新目录
# → serving dir 完整性 → 召回门 ≥0.55 → 原子 swap 进规范路径。零迁移（不碰老 0.6.13 库）。
# 幂等 + flock + fail-loud。SWAP 仅在召回门 PASS 后执行。
#
# 用法：bash full-reingest.sh            # 跑 backup→index→gate（到 gate 停，打印 ready-to-swap）
#       SWAP=1 bash full-reingest.sh     # gate PASS 后再带 SWAP=1 跑一次，执行 swap
# 中断重入：再次运行自动走增量续跑（不重扫已摄入的；--full 仅首次空目录用）
set -euo pipefail
CANON="${CANON_DATA_DIR:-$HOME/.local/share/coding-agent-search}"
NEW="${NEW_DATA_DIR:-$CANON.new}"
URL="${CASS_INFINITY_URL:-http://127.0.0.1:7997}"
BIN="${CASS_BIN:-$HOME/.local/bin/cass-infinity}"
RECALL="${RECALL_RUN:-$HOME/projects/cc-workspace/docs/projects/shared-memory/recall-regression/run.py}"
LOCK="$HOME/.local/share/.cass-reingest.lock"

exec 9>"$LOCK"; flock -n 9 || { echo "another reingest running"; exit 0; }
curl -sf -m5 "$URL/health" >/dev/null || { echo "FAIL: Infinity down"; exit 2; }

# 1) 备份老库（回滚后路；幂等：当日已备份则跳过）
BK="$CANON.0.6.13.bak.$(date +%Y%m%d)"
if [ -d "$CANON" ] && [ ! -e "$BK" ]; then cp -a "$CANON" "$BK"; echo "backed up -> $BK"; else echo "backup skip ($BK or no canon)"; fi

# 2a) 词法 + DB：index（不带 --semantic）。空目录首次 --full；已有内容增量。
#     ⚠ 不用整合 `index --semantic`：phase-2 stall-abort 误杀（upstream #244/#258——语义阶段不推进 current
#     计数，stall 检测器误判卡死 exit 70，实测 2026-06-27 在此栽过）。故词法/语义分两步。
mkdir -p "$NEW"
if [ -f "$NEW/agent_search.db" ] && [ "$(sqlite3 "$NEW/agent_search.db" 'SELECT COUNT(*) FROM conversations' 2>/dev/null || echo 0)" -gt 0 ]; then
  echo "resume 词法: 增量"; CASS_DATA_DIR="$NEW" CASS_INFINITY_URL="$URL" "$BIN" index --json
else
  echo "initial 词法: --full"; CASS_DATA_DIR="$NEW" CASS_INFINITY_URL="$URL" "$BIN" index --full --json
fi

# 2b) bge-m3 语义：backfill 循环至 published（proven 路径，resilient，绕过整合 --semantic 的 stall-abort）
prev=-1; pub=""
for i in $(seq 1 200); do
  out=$(CASS_DATA_DIR="$NEW" CASS_INFINITY_URL="$URL" "$BIN" models backfill --tier quality --embedder infinity --scheduled --batch-conversations 999999 --json 2>>/tmp/cc-reingest-backfill.err) || { echo "FAIL: backfill errored"; exit 1; }
  read -r pub off < <(printf '%s' "$out" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('published'),d.get('last_offset'))") || { echo "FAIL: parse"; exit 1; }
  echo "[semantic iter $i] published=$pub offset=$off"
  [ "$pub" = "True" ] && break
  [ "$off" = "$prev" ] && { echo "FAIL: STALLED at $off"; exit 1; }
  prev=$off
done
[ "$pub" = "True" ] || { echo "FAIL: bge-m3 not published"; exit 1; }

# 3) serving dir 完整性（词法 index/ + bge-m3 .fsvi + manifest ready）
[ -d "$NEW/index" ] && [ -f "$NEW/vector_index/index-bge-m3.fsvi" ] || { echo "FAIL: serving dir 不完整（词法/语义缺）"; exit 1; }
python3 -c "import json,sys;m=json.load(open('$NEW/vector_index/semantic_manifest.json'))['quality_tier'];sys.exit(0 if m.get('ready') and m.get('embedder_id')=='bge-m3' else 1)" \
  || { echo "FAIL: bge-m3 quality tier 未 ready"; exit 1; }

# 4) 召回门 ≥0.55（FAIL 则不 swap）
CASS_DATA_DIR="$NEW" CASS_INFINITY_URL="$URL" python3 "$RECALL" "$BIN" || { echo "FAIL: 召回门未过，不 swap"; exit 1; }
echo "GATE PASSED."

# 5) 原子 swap（仅 SWAP=1）。老库已在步骤1另备份，此处再留一份 .swapped 双保险。
if [ "${SWAP:-0}" = "1" ]; then
  [ -d "$CANON" ] && mv "$CANON" "$CANON.0.6.13.bak.swapped.$(date +%Y%m%d-%H%M%S)"
  mv "$NEW" "$CANON"
  CASS_DATA_DIR="$CANON" "$BIN" status --json | python3 -c "import sys,json;print('canon opened:',json.load(sys.stdin).get('database',{}).get('opened'))"
  echo "OK: swapped -> $CANON 现为 0.6.17 fork 库"
else
  echo "READY TO SWAP: 召回门已过。带 SWAP=1 再跑一次执行 swap，或手动 mv。"
fi
