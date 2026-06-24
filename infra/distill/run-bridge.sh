#!/usr/bin/env bash
# 夜批入口：source 全部 env 后跑桥；stdout = run_batch 的 JSON 报告（最后一行）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # infra/distill → repo root（M1-EXIT ROOT 上溯坑：两级深目录用 ../..）
cd "$ROOT"
export PATH="$HOME/.bun/bin:$PATH"
set -a
source infra/gbrain/config.env
source infra/pg-memory/.env
source infra/distill/config.env
source infra/gbrain/clients.env        # HUB_BRIDGE_CLIENT_ID/SECRET
set +a
export CASS_CANON_DB="${CASS_CANON_DB:-$HOME/.local/share/coding-agent-search/agent_search.db}"
export BRIDGE_TG_NOTIFY=1   # 生产夜批显式开 TG 异常告警（测试/默认不开，见 report._tg_send）
exec uv run python -m distill.run
