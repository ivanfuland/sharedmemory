#!/usr/bin/env bash
# 探测 CASS canonical schema：验 sqlite3 兼容 + dump schema + messages 样本。
# 用法：probe-cass-schema.sh <canonical_db>
set -euo pipefail
DB="${1:?需要 canonical db 路径}"
echo "=== sqlite3 兼容性 ==="
sqlite3 "$DB" '.schema' >/dev/null 2>&1 && echo "COMPAT: OK" || { echo "COMPAT: FAIL → raw fallback (Task 3b)"; exit 10; }
echo "=== 表清单 ==="; sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
echo "=== messages/conversations/agents/workspaces schema ==="
sqlite3 "$DB" "SELECT sql FROM sqlite_master WHERE name IN ('messages','conversations','agents','workspaces');"
