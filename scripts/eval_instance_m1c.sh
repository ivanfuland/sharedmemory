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
      EVEROS_RERANK__MODEL="${EVEROS_RERANK_MODEL:?env.sh 需补 EVEROS_RERANK_MODEL(以副本 everos.toml/Infinity 模型清单核实)}" \
      setsid "$EVEROS_BIN" server start --root "$ROOT" \
        > "$WORK/server.log" 2>&1 &
    echo $! > "$WORK/server.pid"; sleep 3
    curl -sf "http://127.0.0.1:$PORT/docs" > /dev/null && echo "up on :$PORT" || { echo "启动失败,看 $WORK/server.log"; exit 1; }
    # 真实检索 smoke:embedding/rerank 组件 guard 缺配置会在这里 fail-loud(不能只看 /docs)
    curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/memory/search" -H 'Content-Type: application/json' \
      -d '{"agent_id":"everos-m1b-probe","query":"smoke","method":"hybrid","top_k":5,"enable_llm_rerank":false}' \
      > /dev/null && echo "search smoke ok" || { echo "search smoke 失败(组件 guard/配置),看 $WORK/server.log"; exit 1; }
    ;;
  stop)   kill -TERM -- -"$(ps -o pgid= -p "$(cat "$WORK/server.pid")" | tr -d ' ')" && echo stopped ;;
  status) curl -sf "http://127.0.0.1:$PORT/docs" >/dev/null && echo up || echo down ;;
esac
