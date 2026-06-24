#!/usr/bin/env bash
# §11.3：状态库 restore smoke——sqlite 备份 → 还原 → 调用方验计数。
# 用法：restore-bridge-smoke.sh <SRC_DB> <DST_DB>
# 退出码：0 = 成功；非零 = 失败（源库不存在 / sqlite 错误 / 可读性校验失败）
set -euo pipefail

SRC="${1:?usage: $0 <src_db> <dst_db>}"
DST="${2:?usage: $0 <src_db> <dst_db>}"

if [ ! -f "$SRC" ]; then
    echo "ERROR: source db not found: $SRC" >&2
    exit 1
fi

# 原子备份（优于 cp，避开写中态）
sqlite3 "$SRC" ".backup '$DST'"

# 可读校验：raw_work_item 和 cursor 表可访问
sqlite3 "$DST" "SELECT count(*) FROM raw_work_item;" >/dev/null
sqlite3 "$DST" "SELECT count(*) FROM cursor;" >/dev/null

echo "restore-bridge-smoke OK: $SRC -> $DST"
