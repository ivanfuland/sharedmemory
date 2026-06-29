#!/usr/bin/env bash
# 夜批入口：source 全部 env 后跑桥；stdout = run_batch 的 JSON 报告（最后一行）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # infra/distill → repo root（M1-EXIT ROOT 上溯坑：两级深目录用 ../..）
cd "$ROOT"
export PATH="$HOME/.bun/bin:$PATH"
_CALLER_TG_NOTIFY="${BRIDGE_TG_NOTIFY-__unset__}"   # 记住调用方显式值(bulk-drain 传 0)，防 source config.env 意外覆盖(P1-B)
set -a
source infra/gbrain/config.env
source infra/pg-memory/.env
source infra/distill/config.env
source infra/gbrain/clients.env        # HUB_BRIDGE_CLIENT_ID/SECRET
set +a
export CASS_CANON_DB="${CASS_CANON_DB:-$HOME/.local/share/coding-agent-search/agent_search.db}"
# 夜批默认开 TG 告警；调用方显式值(bulk-drain 放量传 0 静默)优先于 config.env(P1-B)
if [ "$_CALLER_TG_NOTIFY" != "__unset__" ]; then
  export BRIDGE_TG_NOTIFY="$_CALLER_TG_NOTIFY"
else
  export BRIDGE_TG_NOTIFY="${BRIDGE_TG_NOTIFY:-1}"
fi
exec uv run python -m distill.run
