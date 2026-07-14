#!/usr/bin/env bash
# M1c Phase 1 评估实例生命周期。用法: eval_instance_m1c.sh {setup|start|stop|status}
# 铁律: 原始 pro-instance 只读;admin key 绝不注入 EverOS 进程(env -i 白名单)。
set -euo pipefail
DATA=~/everos-m1b-data
WORK="$DATA/m1c-eval"
ROOT="$WORK/eval-workdir"
ENVSH="$DATA/env.sh"
PORT=8010
# everos CLI 不在 PATH(2026-07-14 实测),用 EverOS 源码环境的入口。
EVEROS_BIN="$HOME/projects/EverOS/.venv/bin/everos"

case "${1:?setup|start|stop|status}" in
  setup)
    mkdir -p "$WORK"
    [ -e "$ROOT" ] && { echo "eval-workdir 已存在,先手动处置(trash),不自动覆盖"; exit 1; }
    cp -a "$DATA/pro-instance" "$ROOT"
    # 原始目录内容级 manifest(收尾 sha256sum -c 验未变,mtime 不可信)
    (cd "$DATA/pro-instance" && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$WORK/pro-instance.sha256"
    echo "copied. manifest: $WORK/pro-instance.sha256 ($(wc -l < "$WORK/pro-instance.sha256") files)"
    ;;
  start)
    # shellcheck disable=SC1090
    source "$ENVSH"   # 只为取变量;下面 env -i 白名单注入,admin key 不进白名单
    env -i HOME="$HOME" PATH="$PATH" \
      EVEROS_ROOT="$ROOT" \
      EVEROS_API__HOST=127.0.0.1 EVEROS_API__PORT=$PORT \
      EVEROS_EMBEDDING__BASE_URL="$INFINITY_BASE" \
      EVEROS_EMBEDDING__MODEL="${EVEROS_EMBED_MODEL:?env.sh 需补 EVEROS_EMBED_MODEL(M1b 用 bge-m3,以副本 everos.toml 现值核实)}" \
      EVEROS_EMBEDDING__API_KEY="${EVEROS_EMBED_API_KEY:-dummy}" \
      EVEROS_RERANK__PROVIDER=vllm EVEROS_RERANK__BASE_URL="$INFINITY_BASE" \
      EVEROS_LLM__MODEL="${EVEROS_LLM_MODEL:?env.sh 缺 EVEROS_LLM_MODEL}" \
      EVEROS_LLM__API_KEY="${EVEROS_M1B_KEY:?env.sh 缺 EVEROS_M1B_KEY}" \
      EVEROS_LLM__BASE_URL="${LITELLM_LLM_BASE:?env.sh 缺 LITELLM_LLM_BASE}" \
      EVEROS_RERANK__MODEL="${EVEROS_RERANK_MODEL:?env.sh 需补 EVEROS_RERANK_MODEL(以副本 everos.toml/Infinity 模型清单核实)}" \
      setsid bash -c 'echo $$ > "$1/server.pid"; exec "$2" server start --root "$3"' _ "$WORK" "$EVEROS_BIN" "$ROOT" \
        > "$WORK/server.log" 2>&1 &
    # /docs 在本配置不存在(实证);轮询真实 search endpoint 探活,最多 60s(启动含 cascade 扫描)
    for i in $(seq 1 30); do
      sleep 2
      if curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/memory/search" -H 'Content-Type: application/json'         -d '{"agent_id":"everos-m1b-probe","query":"probe","method":"keyword","top_k":1,"enable_llm_rerank":false}' > /dev/null; then
        echo "up on :$PORT (after $((i*2))s)"; break
      fi
      [ "$i" = 30 ] && { echo "启动失败(60s 未就绪),看 $WORK/server.log"; exit 1; }
    done
    # 真实检索 smoke:embedding/rerank 组件 guard 缺配置会在这里 fail-loud(不能只看 /docs)
    curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/memory/search" -H 'Content-Type: application/json' \
      -d '{"agent_id":"everos-m1b-probe","query":"smoke","method":"hybrid","top_k":5,"enable_llm_rerank":false}' \
      > /dev/null && echo "search smoke ok" || { echo "search smoke 失败(组件 guard/配置),看 $WORK/server.log"; exit 1; }
    ;;
  stop)   kill -TERM -- -"$(ps -o pgid= -p "$(cat "$WORK/server.pid")" | tr -d ' ')" && echo stopped ;;
  status)
    if kill -0 "$(cat "$WORK/server.pid" 2>/dev/null)" 2>/dev/null; then
      curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/memory/search" -H 'Content-Type: application/json'         -d '{"agent_id":"everos-m1b-probe","query":"probe","method":"keyword","top_k":1,"enable_llm_rerank":false}'         > /dev/null && echo "up (pid $(cat "$WORK/server.pid"))" || echo "process alive but api down"
    else echo down; fi ;;
esac
