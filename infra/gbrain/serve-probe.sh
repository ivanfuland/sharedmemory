#!/usr/bin/env bash
# serve-probe.sh — 快速验证 serve --http 行为（开发/运维用，不进 CI）。
# 用法：bash infra/gbrain/serve-probe.sh
# 前提：先跑 register-clients.sh，clients.env 已存在。
set -euo pipefail
export PATH="$HOME/.bun/bin:$PATH"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export GBRAIN_HOME="$ROOT/sandbox/gbrain-pg"
set -a; source "$ROOT/infra/gbrain/config.env"; source "$ROOT/infra/pg-memory/.env"; set +a
CLIENTS_ENV="$ROOT/infra/gbrain/clients.env"
[ -f "$CLIENTS_ENV" ] || { echo "缺 clients.env，先跑 register-clients.sh"; exit 1; }
set -a; source "$CLIENTS_ENV"; set +a

PORT=7798
BASE="http://127.0.0.1:${PORT}"

echo "=== 启动 serve --http :${PORT} ==="
gbrain serve --http --port "$PORT" &
SERVE_PID=$!
trap 'kill "$SERVE_PID" 2>/dev/null || true' EXIT

for i in $(seq 1 30); do
  curl -sf "$BASE/health" > /dev/null 2>&1 && echo "READY (${i}×0.5s)" && break || sleep 0.5
done
curl -sf "$BASE/health" | python3 -m json.tool

echo ""
echo "=== OAuth token exchange (hub-cc) ==="
TOKEN=$(curl -sf -X POST "$BASE/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&client_id=${HUB_CC_CLIENT_ID}&client_secret=${HUB_CC_CLIENT_SECRET}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['access_token'])")
echo "access_token: ${TOKEN:0:30}..."

echo ""
echo "=== put_page → hub-cc (正例) ==="
curl -s -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"put_page","arguments":{"slug":"probe/serve-probe","content":"# serve probe\n生成时间: '"$(date)"'\n"}},"id":1}'

echo ""
echo ""
echo "=== put_page → read-only client (负例，期望 insufficient_scope) ==="
RO_TOKEN=$(curl -sf -X POST "$BASE/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&client_id=${HUB_READONLY_CLIENT_ID}&client_secret=${HUB_READONLY_CLIENT_SECRET}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['access_token'])")
curl -s -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $RO_TOKEN" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"put_page","arguments":{"slug":"probe/ro-test","content":"# nope\n"}},"id":2}'

echo ""
echo ""
echo "=== 探测完成，关 serve ==="
