#!/usr/bin/env bash
# token-refresh.sh — re-mint a client's client_credentials access_token,
# atomic write to ~/.config/gbrain/<client>.token.
#
# Usage: token-refresh.sh [CLIENT_PREFIX]
#   CLIENT_PREFIX: key prefix in clients.env (default: HUB_CC)
#   Examples: HUB_CC / HUB_CODEX / HUB_OPENCLAW
#
# Called by: gbrain-token-refresh.service (daily slow-rotation)
# Side-effects: writes ~/.config/gbrain/<client>.token (harmless token cache,
#   NOT a host-config flip). Token TTL=30d so live sessions are unaffected.

set -euo pipefail

CLIENT="${1:-HUB_CC}"   # clients.env 的前缀，如 HUB_CC / HUB_CODEX / HUB_OPENCLAW
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

# Load credentials
set -a; source "$ROOT/infra/gbrain/clients.env"; set +a

cid_var="${CLIENT}_CLIENT_ID"
cs_var="${CLIENT}_CLIENT_SECRET"

# Validate vars exist
if [[ -z "${!cid_var:-}" || -z "${!cs_var:-}" ]]; then
    echo "FATAL: 未找到 ${cid_var} / ${cs_var}（infra/gbrain/clients.env）" >&2
    exit 1
fi

# Mint a fresh access_token
TOK="$(curl -fsS -X POST http://127.0.0.1:7777/token \
  -d grant_type=client_credentials \
  -d "client_id=${!cid_var}" \
  -d "client_secret=${!cs_var}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["access_token"])')"

if [[ -z "$TOK" ]]; then
    echo "FATAL: 没换到 token（$CLIENT）" >&2
    exit 1
fi

# Atomic write to ~/.config/gbrain/<client>.token
# Client prefix HUB_CC → filename hub-cc.token (lowercase, underscores→hyphens)
FNAME="$(echo "$CLIENT" | tr 'A-Z_' 'a-z-').token"
OUT="$HOME/.config/gbrain/$FNAME"
mkdir -p "$(dirname "$OUT")"
(umask 077; printf '%s' "$TOK" > "${OUT}.tmp" && mv -f "${OUT}.tmp" "$OUT")

echo "refreshed $CLIENT → $OUT"
