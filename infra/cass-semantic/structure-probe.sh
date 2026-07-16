#!/usr/bin/env bash
# CASS 主库结构探针——2026-07-16 两类已知损坏签名的小时级哨兵(fsqlite 丢写 bug 取证产物)。
# 签名 A: rowid 空洞(COUNT != MAX;REPLACE 烧号或行丢失都会触发,前者良性但会引发语义全量回落,
#         后者是数据丢失——都值得 1 小时内知道,而不是等夜备五腿门)。
# 签名 B: seek 不可达(全表扫描可见但按 id 点查不中 = b-tree 分隔键陈旧,07-09/07-16 同款)。
# 纯只读;全量 anti-join(内层逐行 seek)健康库实测秒级。exit 0 = 干净;exit 1 = 发现签名;
# exit 2+ = 探针自身故障(超时/sqlite 错),同样 fail-loud——探针跑不完本身就是红旗。
# codex R2-F1:每条查询硬超时 60s,坏库/IO 退化下绝不占满小时窗(外层另有兜底超时)。
# codex R2-F2:用命令替换拿结果(set -e 保退出码),不用进程替换+read(会吞生产者非零退出)。
# 用法: structure-probe.sh <db 路径>
set -euo pipefail
DB="${1:?usage: structure-probe.sh <agent_search.db>}"
Q_TIMEOUT="${STRUCTURE_PROBE_QUERY_TIMEOUT:-60}"

q() { timeout --kill-after=10 "$Q_TIMEOUT" sqlite3 -readonly "$DB" "$1"; }   # kill-after:TERM 被忽略也硬杀

fail=0
for t in conversations messages; do
  # 签名 A(q 失败/超时 → set -e 以非零退出,exit≥2 语义)
  row=$(q "SELECT COUNT(*) || ' ' || COALESCE(MAX(id),0) FROM $t;")
  cnt=${row%% *}; mx=${row##* }
  if [ "$cnt" != "$mx" ]; then
    echo "STRUCTURE_PROBE_FAIL table=$t signature=gap COUNT=$cnt MAX=$mx"
    fail=1
  fi
  # 签名 B: 外层 id+0 强制全扫,内层等值走 b-tree seek;seek 不可达的行会被计数
  invis=$(q "SELECT COUNT(*) FROM $t a WHERE a.id+0 > 0 AND NOT EXISTS (SELECT 1 FROM $t b WHERE b.id = a.id);")
  if [ "$invis" != "0" ]; then
    echo "STRUCTURE_PROBE_FAIL table=$t signature=seek-invisible rows=$invis"
    fail=1
  fi
done
if [ "$fail" = 0 ]; then echo "structure probe clean"; fi
exit "$fail"
