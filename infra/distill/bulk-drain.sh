#!/usr/bin/env bash
# bulk-drain：放量追平积压。反复跑核心 run-bridge.sh，直到一个 batch 不再 distill 任何 span
# （processed_count==0 = 积压清空 + CASS 未读历史读穿）。
#
# 与 nightly 共用同一核心 run-bridge.sh，区别仅两点：
#   ① BRIDGE_TG_NOTIFY=0 静默——放量阶段不逐批 TG 告警（starved 每批都在，会轰炸）。
#   ② 外层循环连跑多批提速。并发不变（distill_concurrency=1，保崩溃恢复语义）——
#      提速靠多跑批，不加线程碰恢复语义。
#
# 用法：bash infra/distill/bulk-drain.sh [MAX_ROUNDS]   （MAX_ROUNDS 默认 40，安全上限防失控）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT"
MAX_ROUNDS="${1:-40}"
ERRLOG="/tmp/cc-bulk-drain.stderr"
round=0
while (( round < MAX_ROUNDS )); do
  round=$((round + 1))
  ts="$(date -u +%H:%M:%S)"
  # 静默跑核心；stdout 末行 = report JSON。run-bridge.sh 非 0 退出（如 deferred_hard_cap 停桥）→ 停。
  set +e
  out="$(BRIDGE_TG_NOTIFY=0 bash infra/distill/run-bridge.sh 2>"$ERRLOG")"
  rc=$?
  set -e
  rep="$(printf '%s\n' "$out" | tail -1)"
  if (( rc != 0 )); then
    echo "[$ts] round $round: run-bridge.sh 退出码 $rc（可能 deferred_hard_cap 停桥需人工）→ 停。stderr 末尾："
    tail -5 "$ERRLOG"
    exit "$rc"
  fi
  read -r processed backlog newq < <(printf '%s' "$rep" | python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
    rec = d.get("reconciled", {})
    newq = d.get("raw_quarantined_new", 0) + rec.get("quarantined", 0) + rec.get("review", 0)
    print(d.get("processed_count", "ERR"), d.get("total_backlog", {}).get("total_backlog", "?"), newq)
except Exception:
    print("ERR ? ?")
')
  echo "[$ts] round $round/$MAX_ROUNDS: processed=$processed backlog=$backlog new_quarantine=$newq"
  if [[ "$processed" == "ERR" ]]; then
    echo "  ⚠️ report JSON 解析失败 → 停（rep 头部：${rep:0:200}）"; exit 1
  fi
  if [[ "$processed" == "0" ]]; then
    # 收敛 = 本批既没 distill 也没新增 quarantine。若 0 distill 但有新增 quarantine，
    # 多半是当批全批蒸馏失败（如 flash 格式崩）被静默压掉——非收敛，loud 停桥待人工（P1-A）。
    if [[ "$newq" != "0" ]]; then
      echo "  ⛔ 本批 0 distill 但新增 $newq 条 quarantine（疑似全批蒸馏失败，如 flash 格式崩）→ 非收敛，停。请查 audit.log + report。"
      exit 2
    fi
    echo "  ✅ 收敛：本批 0 distill、0 新增 quarantine（积压清空 + CASS 读穿）"; break
  fi
done
if (( round >= MAX_ROUNDS )); then
  echo "⚠️ 达 MAX_ROUNDS=$MAX_ROUNDS 仍未收敛（积压可能仍大）→ 再跑一次 bulk-drain 继续，或调高上限。"
fi
echo "=== bulk-drain 结束：$round 轮 ==="
