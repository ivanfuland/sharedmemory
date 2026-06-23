#!/usr/bin/env bash
# 幂等注册 M2 sources（已存在跳过）+ OAuth client_credentials clients（同名先撤后建，无孤儿残留）。
# 产物 client_secret 写 infra/gbrain/clients.env(600,gitignore)；secret 只在注册时出现一次。
#
# Probe 校正（2026-06-23）：
#   - sources add --path 必填，脚本为每 source 建占位目录 sandbox/brain-sources/<id>
#   - register-client --federated-read 逗号分隔列表（实测可接受 a,b,c,d,e 含 default）
#   - sed 解析 "  Client ID:  <id>" / "  Client Secret:  <secret>" 格式已验证
#   - oauth_clients 无 client_name UNIQUE 约束 → revoke_by_name 先查 DB 全撤再建
set -euo pipefail
export PATH="$HOME/.bun/bin:$PATH"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export GBRAIN_HOME="$ROOT/sandbox/gbrain-pg"
set -a; source "$ROOT/infra/gbrain/config.env"; source "$ROOT/infra/pg-memory/.env"; set +a

SOURCES=(hub-cc hub-codex hub-openclaw distill-bridge)
ALL_FED="hub-cc,hub-codex,hub-openclaw,distill-bridge,default"   # 读联邦 = 全部 source
SRC_DIR="$ROOT/sandbox/brain-sources"; mkdir -p "$SRC_DIR"
CLIENTS_ENV="$ROOT/infra/gbrain/clients.env"; : > "$CLIENTS_ENV"; chmod 600 "$CLIENTS_ENV"

existing_sources() { gbrain sources list 2>/dev/null | awk 'NR>2{print $1}'; }
have_source() { existing_sources | grep -qx "$1"; }

for s in "${SOURCES[@]}"; do
  mkdir -p "$SRC_DIR/$s"
  have_source "$s" || gbrain sources add "$s" --path "$SRC_DIR/$s"
done

# 幂等真义：先按 client_name 撤旧 client（schema 无 client_name UNIQUE，重跑不撤会留孤儿 active client）。
# revoke-client 吃 client_id，故先查 DB 拿同名 client_id 全撤。
revoke_by_name() {  # revoke_by_name <name>
  local name="$1" cid
  while read -r cid; do
    [ -n "$cid" ] && gbrain auth revoke-client "$cid" >/dev/null 2>&1 || true
  done < <(docker exec pg-memory psql -U gbrain -d gbrain -tA \
            -c "SELECT client_id FROM oauth_clients WHERE client_name='$name' AND deleted_at IS NULL;" 2>/dev/null)
}

# 注册 client：每 source 一个 read+write（写限本 source、读联邦全部）+ 一个全局 read-only（负例用）
reg() {  # reg <name> <scopes> <source> <federated|->
  local name="$1" scopes="$2" src="$3" fed="$4"
  revoke_by_name "$name"   # 幂等：撤同名旧 client 再建，避免孤儿残留
  local args=(auth register-client "$name" --grant-types client_credentials --scopes "$scopes" --source "$src")
  [ "$fed" != "-" ] && args+=(--federated-read "$fed")
  local out; out="$(gbrain "${args[@]}")"
  local cid cs
  cid="$(printf '%s\n' "$out" | sed -n 's/.*Client ID: *//p' | tr -d ' ')"
  cs="$(printf '%s\n' "$out"  | sed -n 's/.*Client Secret: *//p' | tr -d ' ')"
  [ -n "$cid" ] || { echo "FATAL: 没解析到 client_id ($name)"; echo "$out"; exit 1; }
  printf '%s_CLIENT_ID=%s\n%s_CLIENT_SECRET=%s\n' \
    "$(echo "$name" | tr 'a-z-' 'A-Z_')" "$cid" \
    "$(echo "$name" | tr 'a-z-' 'A-Z_')" "$cs" >> "$CLIENTS_ENV"
}

reg hub-cc         "read write" hub-cc         "$ALL_FED"
reg hub-codex      "read write" hub-codex      "$ALL_FED"
reg hub-openclaw   "read write" hub-openclaw   "$ALL_FED"
reg hub-bridge     "read write" distill-bridge "$ALL_FED"   # M3 蒸馏桥 service client
reg hub-readonly   "read"       default        "$ALL_FED"   # 负例：只读端
reg hub-shortlived "read"       default        "$ALL_FED"   # Task7 expiry 测试用，下行 SQL 置短 TTL
# register-client 无 --token-ttl flag（codex R2#new1）；token_ttl 是 DB 列，SQL 显式置：
# 生产 harness token_ttl=30d（远超任何会话 → live MCP 会话内 token 永不过期，解 codex R3 Judgment b BLOCKER）
# ★ 断言影响 4 行（codex R4#new2：>/dev/null 会让 silent no-op 假绿）——CTE RETURNING + count
UPD="$(docker exec pg-memory psql -U gbrain -d gbrain -tA -c \
  "WITH u AS (UPDATE oauth_clients SET token_ttl=2592000 WHERE client_name IN ('hub-cc','hub-codex','hub-openclaw','hub-bridge') AND deleted_at IS NULL RETURNING 1) SELECT count(*) FROM u;")"
[ "$UPD" = "4" ] || { echo "FATAL: prod token_ttl UPDATE 影响 $UPD 行（期望 4）——client 名/状态不符"; exit 1; }
# hub-shortlived=2s（仅供 expiry-recovery 机制测试）
docker exec pg-memory psql -U gbrain -d gbrain -c \
  "UPDATE oauth_clients SET token_ttl=2 WHERE client_name='hub-shortlived' AND deleted_at IS NULL;" >/dev/null
echo "clients 写入 $CLIENTS_ENV（600）。secret 不再展示，丢了重 register。"
