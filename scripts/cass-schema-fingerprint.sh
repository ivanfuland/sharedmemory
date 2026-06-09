#!/usr/bin/env bash
# CASS canonical schema 指纹：覆盖 read JOIN 触及的表（messages/conversations/agents/workspaces）。
# 任一表结构变更即变 → 蒸馏桥启动比对，不匹配拒绝运行。
# 用法：cass-schema-fingerprint.sh <canonical_db>
set -euo pipefail
DB="${1:?需要 canonical db 路径}"
sqlite3 "$DB" "SELECT sql FROM sqlite_master WHERE type='table'
  AND name IN ('messages','conversations','agents','workspaces')
  ORDER BY name;" | tr -s ' \t\n' ' ' | sha256sum | awk '{print $1}'
