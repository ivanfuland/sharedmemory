#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"

echo "[install-service] 生成 systemd-safe env…"
# 1) 生成 systemd-safe env：bash 正确 source（处理行内注释/引号），再 re-emit 干净 KEY=value。
#    直接 EnvironmentFile 原始 config.env 会把 `URL  # comment` 的行内注释吃进值（codex R1 #5）。
set -a; source "$ROOT/infra/gbrain/config.env"; source "$ROOT/infra/pg-memory/.env"; set +a
GEN="$HERE/env.generated"; : > "$GEN"; chmod 600 "$GEN"
for k in OPENROUTER_BASE_URL OPENROUTER_API_KEY LLAMA_SERVER_RERANKER_BASE_URL DEEPSEEK_API_KEY POSTGRES_PASSWORD; do
  v="${!k:-}"; [ -n "$v" ] || { echo "FATAL: env $k 空，先填 config.env / pg-memory/.env"; exit 1; }
  printf '%s=%s\n' "$k" "$v" >> "$GEN"
done
echo "[install-service] env.generated 写入完成（600）"

# 2) 装 unit
DEST="$HOME/.config/systemd/user/gbrain-mcp.service"
mkdir -p "$(dirname "$DEST")"
cp "$HERE/gbrain-mcp.service" "$DEST"
echo "[install-service] unit 复制到 $DEST"
systemctl --user daemon-reload
systemctl --user enable --now gbrain-mcp.service
echo "[install-service] 服务已 enable + start"

# 3) 等 /health
echo "[install-service] 等待 /health 就绪（最多 10s）…"
ready=0
for _ in $(seq 1 20); do curl -fsS http://127.0.0.1:7777/health >/dev/null 2>&1 && { ready=1; break; }; sleep 0.5; done
[ "$ready" = 1 ] || { echo "FATAL: /health 不通，看 journalctl --user -u gbrain-mcp -n50"; exit 1; }
echo "[install-service] /health OK"

# 4) ★ 真验嵌入路径 through systemd serve 进程的 /mcp（不是本地 CLI——CLI 用 shell env，证不了 unit env）
#    需 Task1 已注册 clients；query 工具名/参数 probe 确认：tool=query, arg=query（task-1-report.md + probe）
[ -f "$ROOT/infra/gbrain/clients.env" ] || { echo "FATAL: 缺 clients.env，先跑 Task1 register-clients.sh"; exit 1; }
set -a; source "$ROOT/infra/gbrain/clients.env"; set +a
TOK="$(curl -fsS -X POST http://127.0.0.1:7777/token \
  -d "grant_type=client_credentials&client_id=$HUB_CC_CLIENT_ID&client_secret=$HUB_CC_CLIENT_SECRET" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')"
QOUT="$(curl -fsS -X POST http://127.0.0.1:7777/mcp \
  -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"query","arguments":{"query":"光合作用"}},"id":1}')"
# query 工具参数是 query（probe 确认 task-1-report + tools/list），非 question
# MCP error 走 result.isError=true 格式（task-1-report §1.4）
if printf '%s' "$QOUT" | grep -qE '"isError"[[:space:]]*:[[:space:]]*true|^.*"error"[[:space:]]*:[[:space:]]*\{'; then
  echo "FATAL: 服务进程 /mcp query 报错——unit env(OPENROUTER_*) 可能被行内注释污染或 LiteLLM 不可达: $QOUT"; exit 1
fi
echo "[install-service] /mcp query 嵌入路径通过"
echo ""
echo "gbrain-mcp 就绪 :7777（/health + 服务进程 /mcp query 嵌入路径双通）"
echo "运维: systemctl --user {start,stop,restart,status} gbrain-mcp"
echo "日志: journalctl --user -u gbrain-mcp -f"
