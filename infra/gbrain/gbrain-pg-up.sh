#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.bun/bin:$PATH"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # 脚本在 infra/gbrain/ 两级深，上溯两级到 repo 根
export GBRAIN_HOME="$ROOT/sandbox/gbrain-pg"
set -a; source "$ROOT/infra/gbrain/config.env"; set +a          # OPENROUTER_BASE_URL + OPENROUTER_API_KEY
set -a; source "$ROOT/infra/pg-memory/.env"; set +a             # POSTGRES_PASSWORD
mkdir -p "$GBRAIN_HOME"
# 密码 URL 编码：DSN 里 @/:/?/#/% 等会破坏 URI；纯字母数字无影响，含特殊字符也安全（codex PR review）
ENC_PW="$(python3 -c "import urllib.parse,os;print(urllib.parse.quote(os.environ['POSTGRES_PASSWORD'],safe=''))")"
# openrouter recipe（openai-compatible）嵌入走 createOpenAICompatible(baseURL=OPENROUTER_BASE_URL) → LiteLLM；
# 模型名 text-embedding-3-small 透传给 LiteLLM；default_dims=1536 preflight 放行
[ -f "$GBRAIN_HOME/.gbrain/config.json" ] || gbrain init \
  --url "postgresql://gbrain:${ENC_PW}@127.0.0.1:5433/gbrain" \
  --embedding-model "openrouter:text-embedding-3-small" --embedding-dimensions 1536 --skip-embed-check
echo "Postgres GBRAIN_HOME=$GBRAIN_HOME 就绪（embed=openrouter:text-embedding-3-small@1536 → LiteLLM via OPENROUTER_BASE_URL）"
