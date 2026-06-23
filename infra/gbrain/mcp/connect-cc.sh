#!/usr/bin/env bash
# connect-cc.sh — re-mint HUB_CC token then wire gbrain MCP into CC's .mcp.json.
#
# ★ DEFERRED ACTIVATION — DO NOT RUN THIS SCRIPT AUTONOMOUSLY ★
# Running this script executes `gbrain connect --install` which edits
# ~/.claude/.mcp.json (CC's live MCP config). This is an activation step
# deferred to Ivan's explicit activation checklist. CC must NOT invoke this
# script automatically; it is only run:
#   1. Manually by Ivan in a terminal when activating the gbrain MCP connection.
#   2. By gbrain-token-refresh.service (after Ivan installs & enables the timer).
#
# Usage: bash connect-cc.sh
# Side-effects: writes ~/.config/gbrain/hub-cc.token + edits ~/.claude/.mcp.json

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.bun/bin:$PATH"

# Step 1: ensure a fresh token in ~/.config/gbrain/hub-cc.token
"$HERE/token-refresh.sh" HUB_CC

# Step 2: read that token
TOK="$(cat "$HOME/.config/gbrain/hub-cc.token")"

# Step 3: install into CC's .mcp.json
# --install: writes the server entry to .mcp.json (required; without it,
#   gbrain connect only prints the config block and does NOT persist it)
# --yes: skip confirmation prompt
# --force: replace an existing same-named server entry
gbrain connect http://127.0.0.1:7777/mcp --token "$TOK" --install --yes --force

echo "CC 已接 gbrain MCP（scoped hub-cc，token_ttl=30d）。"
echo "timer 每日慢轮转重写 .mcp.json（--install）；live 会话内 token 不过期（30d ≫ 会话）。"
