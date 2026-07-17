#!/usr/bin/env bash
# Task 9:故障注入套件入口——薄 bash 包装,真正的逐案例编排/断言在
# scripts/everos_mcp_faults.py(subprocess 管理 + HTTP stub + 断言用 Python
# 写更可靠,bash 只负责定位仓库根 + 转发退出码)。
#
# 全部案例跑在隔离目录/ephemeral 端口/假 docker 下,不碰生产 ledger/EverOS/
# Infinity/docker 容器。逐项 PASS/FAIL 见 stdout,最终一行给出 "N/M PASS" 汇总。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

exec uv run --frozen --group mcp-shadow python -m scripts.everos_mcp_faults "$@"
