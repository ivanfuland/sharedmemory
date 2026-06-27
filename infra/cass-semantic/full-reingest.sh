#!/usr/bin/env bash
# CASS 单版本全新重摄入：备份老库 → fork 全量 index(JSONL→DB+词法+bge-m3 语义) 进临时新目录
# → serving dir 完整性 → 召回门 ≥0.55 → 原子 swap 进规范路径。零迁移（不碰老 0.6.13 库）。
# 幂等 + flock + fail-loud。SWAP 仅在召回门 PASS 后执行。
#
# 用法：bash full-reingest.sh            # 跑 backup→index→gate（到 gate 停，打印 ready-to-swap）
#       SWAP=1 bash full-reingest.sh     # gate PASS 后再带 SWAP=1 跑一次，执行 swap
# 中断重入：rm "$NEW" 后重跑（fresh）。摄入用 --full 探停(external_id 去重幂等)，词法走 force-rebuild。
set -euo pipefail
CANON="${CANON_DATA_DIR:-$HOME/.local/share/coding-agent-search}"
NEW="${NEW_DATA_DIR:-$CANON.new}"
URL="${CASS_INFINITY_URL:-http://127.0.0.1:7997}"
BIN="${CASS_BIN:-$HOME/.local/bin/cass-infinity}"
RECALL="${RECALL_RUN:-$HOME/projects/cc-workspace/docs/projects/shared-memory/recall-regression/run.py}"
LOCK="$HOME/.local/share/.cass-write.lock"   # 全局写锁，与 index-pull.sh 共用（codex P1#1：防 pull 在 swap 窗口写 canonical）

exec 9>"$LOCK"; flock -n 9 || { echo "another cass write holds lock (reingest/pull)"; exit 0; }
curl -sf -m5 "$URL/health" >/dev/null || { echo "FAIL: Infinity down"; exit 2; }

# 1) 备份老库（回滚后路；幂等：当日已备份则跳过）
BK="$CANON.0.6.13.bak.$(date +%Y%m%d)"
if [ -d "$CANON" ] && [ ! -e "$BK" ]; then cp -a "$CANON" "$BK"; echo "backed up -> $BK"; else echo "backup skip ($BK or no canon)"; fi

# 2a) 摄入 + 词法。⚠ 绝不用 `index --full` 一把梭：它的并行 Tantivy rebuild 管线在 finalize 阶段
#     会因内存压力降档**死锁**（upstream #244；实测 2026-06-27 连栽两次，tantivy_segment_files=0、
#     current 计数永不推进、120s 零进展）。可靠流程分两步：
#       ① `--full` 只借来「摄入 JSONL→DB」(indexing 阶段正常)，探到 conv 数稳定即停（不等它死锁的 finalize）
#       ② 词法走 `index --force-rebuild`（从已摄入 DB 重建 Tantivy 的轻量路径，不走死锁管线，实测 6s）
mkdir -p "$NEW"
export CASS_INDEX_STALL_ABORT_SECS=0   # stall 检测降为只报告(留日志)；死锁靠下面"探停"处理，不靠它中止

# ① 摄入：--full 后台跑，轮询 conv 数稳定即停 --full
rm -f "$NEW/index-run.lock" "$NEW/index-run.lock.meta" 2>/dev/null || true
CASS_DATA_DIR="$NEW" CASS_INFINITY_URL="$URL" "$BIN" index --full --json >/tmp/cc-reingest-ingest.log 2>&1 &
ING=$!
prev=-1; stable=0
while kill -0 "$ING" 2>/dev/null; do
  sleep 8
  cur=$(sqlite3 "$NEW/agent_search.db" 'SELECT COUNT(*) FROM conversations' 2>/dev/null || echo 0)
  if [ "${cur:-0}" -gt 0 ] && [ "$cur" = "$prev" ]; then
    stable=$((stable+1))
    if [ "$stable" -ge 3 ]; then echo "摄入稳定在 $cur conv，停 --full（绕 #244 finalize 死锁）"; kill "$ING" 2>/dev/null || true; break; fi
  else stable=0; fi
  prev="$cur"
done
wait "$ING" 2>/dev/null || true
ncv=$(sqlite3 "$NEW/agent_search.db" 'SELECT COUNT(*) FROM conversations' 2>/dev/null || echo 0)
[ "${ncv:-0}" -gt 0 ] || { echo "FAIL: 摄入无数据"; exit 1; }
echo "摄入完成: $ncv conv"
rm -f "$NEW/index-run.lock" "$NEW/index-run.lock.meta" 2>/dev/null || true   # 清被杀 --full 的残留锁

# ② 词法：force-rebuild（从 DB 重建 Tantivy，绕死锁）
CASS_DATA_DIR="$NEW" CASS_INFINITY_URL="$URL" "$BIN" index --force-rebuild >/tmp/cc-reingest-lexical.log 2>&1 \
  || { echo "FAIL: 词法 force-rebuild"; tail -3 /tmp/cc-reingest-lexical.log; exit 1; }
idxk=$(du -sk "$NEW/index" 2>/dev/null | cut -f1)
[ "${idxk:-0}" -gt 500 ] || { echo "FAIL: 词法索引空/缺 (${idxk}KB)"; exit 1; }
echo "词法建好: $(du -sh "$NEW/index" | cut -f1)"

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

# 5) swap（仅 SWAP=1）。老库已在步骤1另备份，此处再留一份 .swapped 双保险。
#    并发安全：全脚本持全局写锁 → index-pull/另一次 reingest 不能并发写 canonical（codex P1#1 writer 竞争已堵）。
#    残留：两次 mv 间 canonical 短暂不存在（亚毫秒），仅影响并发 READER（search）——失败重试即可，不损坏。
#    单用户 + swap 仅全量重建(罕见/手动)，不值得为此上 renameat2(RENAME_EXCHANGE)。
if [ "${SWAP:-0}" = "1" ]; then
  [ -d "$CANON" ] && mv "$CANON" "$CANON.0.6.13.bak.swapped.$(date +%Y%m%d-%H%M%S)"
  mv "$NEW" "$CANON"
  CASS_DATA_DIR="$CANON" "$BIN" status --json | python3 -c "import sys,json;print('canon opened:',json.load(sys.stdin).get('database',{}).get('opened'))"
  echo "OK: swapped -> $CANON 现为 0.6.17 fork 库"
else
  echo "READY TO SWAP: 召回门已过。带 SWAP=1 再跑一次执行 swap，或手动 mv。"
fi
