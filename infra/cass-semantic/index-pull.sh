#!/usr/bin/env bash
# CASS 增量拉取 entrypoint（Inngest cass-index-daily 调用，镜像 distill/run-bridge.sh）。
# 在规范 canonical data_dir 上跑 fork 增量 index + 语义（scan watermark + tail + memoization → 只摄/嵌新内容）。
# 末行输出 JSON 报告供 runner 解析；非 0 退出 = Inngest 标失败 + TG 告警。
set -euo pipefail
CANON="${CASS_DATA_DIR:-$HOME/.local/share/coding-agent-search}"
URL="${CASS_INFINITY_URL:-http://127.0.0.1:7997}"
BIN="${CASS_BIN:-$HOME/.local/bin/cass-infinity}"
LOCK="$HOME/.local/share/.cass-index-pull.lock"

exec 9>"$LOCK"; flock -n 9 || { echo '{"ok":false,"skipped":"another pull running"}'; exit 0; }
curl -sf -m5 "$URL/health" >/dev/null || { echo '{"ok":false,"error":"Infinity down"}'; exit 2; }
[ -d "$CANON" ] || { echo "{\"ok\":false,\"error\":\"canonical missing: $CANON\"}"; exit 1; }

# 增量 index（默认增量；非 --full）：JSONL→DB + 词法 + bge-m3 语义，只处理新/变内容。
CASS_DATA_DIR="$CANON" CASS_INFINITY_URL="$URL" "$BIN" index --semantic --embedder infinity >/tmp/cc-cass-pull.log 2>&1 \
  || { echo "{\"ok\":false,\"error\":\"index failed\",\"tail\":\"$(tail -3 /tmp/cc-cass-pull.log | tr '\n' ' ' | sed 's/"/'\''/g' | cut -c1-300)\"}"; exit 1; }

# 末行 JSON 报告（conv/msg 计数供观测增量进展）
conv=$(sqlite3 "$CANON/agent_search.db" 'SELECT COUNT(*) FROM conversations' 2>/dev/null || echo -1)
msg=$(sqlite3 "$CANON/agent_search.db" 'SELECT COUNT(*) FROM messages' 2>/dev/null || echo -1)
ready=$(python3 -c "import json;print(str(json.load(open('$CANON/vector_index/semantic_manifest.json'))['quality_tier'].get('ready')).lower())" 2>/dev/null || echo unknown)
printf '{"ok":true,"conversations":%s,"messages":%s,"semantic_ready":"%s"}\n' "$conv" "$msg" "$ready"
