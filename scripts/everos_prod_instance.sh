#!/usr/bin/env bash
# everos-prod 常驻实例生命周期(照 eval_instance_m1c.sh 模式)。用法: {setup|pin|run|smoke|status}
# 常驻由 systemd user unit(everos-prod.service)以 `run` 启动;`run` 校验版本 PIN 后 exec。
# 铁律: 拓扑一律私有 env 注入(PUBLIC 仓零字面量);admin key 绝不进实例 env;
#       版本钉死(spec §3):EverOS 跟随 mutable checkout/venv 漂移必须被启动挡住,
#       PIN 只能经升级门(校准集回归过了)更新——`pin` 子命令写,`run` 子命令验。
set -euo pipefail
ENVSH="${EVEROS_PROD_ENV:?set EVEROS_PROD_ENV to your private env file}"
# shellcheck disable=SC1090
source "$ENVSH"
ROOT="${EVEROS_PROD_ROOT:?env 缺 EVEROS_PROD_ROOT}"
PORT="${EVEROS_PROD_PORT:?env 缺 EVEROS_PROD_PORT}"
TEMPLATE="${EVEROS_TEMPLATE_DIR:?env 缺 EVEROS_TEMPLATE_DIR}"
AGENT="${EVEROS_FEED_AGENT_ID:-everos-prod}"
BASE="$(dirname "$ROOT")"
PIN_FILE="$BASE/PIN"

_fingerprint() {  # 两因子:源码 git SHA + venv 依赖冻结哈希(抓 git 不变但 pip install 变的漂移)
  SRC="${EVEROS_SRC_DIR:?env 缺 EVEROS_SRC_DIR}"
  BIN="${EVEROS_BIN:?env 缺 EVEROS_BIN}"
  PY="$(dirname "$BIN")/python"
  echo "git_sha=$(git -C "$SRC" rev-parse HEAD)"
  echo "venv_freeze_sha256=$("$PY" -m pip freeze 2>/dev/null | sha256sum | cut -d' ' -f1)"
}

_search() {  # 真 search endpoint 探活(M1c 实证:/docs 在本配置不存在)
  curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/memory/search" -H 'Content-Type: application/json' \
    -d "{\"agent_id\":\"$AGENT\",\"query\":\"$1\",\"method\":\"$2\",\"top_k\":1,\"enable_llm_rerank\":false}" \
    > /dev/null
}

case "${1:?setup|pin|run|smoke|status}" in
  setup)
    [ -e "$ROOT" ] && { echo "ROOT 已存在,先手动处置(trash),不自动覆盖"; exit 1; }
    mkdir -p "$ROOT"
    cp "$TEMPLATE/everos.toml" "$TEMPLATE/ome.toml" "$ROOT/"
    echo "fresh root ready: $ROOT(只拷两份 toml——生产从零喂,不带探针数据/探针 agent 分区)"
    ;;
  pin)  # 记录当前版本指纹。只允许两个场合调用:首次落地(Task 16)/升级门通过后(runbook ④)。
    _fingerprint > "$PIN_FILE"
    echo "pinned:"; cat "$PIN_FILE"
    ;;
  run)  # systemd 入口:PIN 校验 fail-closed,不匹配拒绝启动(防绕过校准集回归门的静默漂移)
    [ -f "$PIN_FILE" ] || { echo "FATAL: $PIN_FILE 不存在——先跑 pin(首次)或升级门"; exit 1; }
    SRC="${EVEROS_SRC_DIR:?env 缺 EVEROS_SRC_DIR}"
    # 含未跟踪文件(R2-P1-2):editable install 下未跟踪的新模块 git SHA 和 pip freeze 都抓不到,
    # 却直接可 import——只 allowlist 已知非代码产物 .codegraph/。
    dirty="$(git -C "$SRC" status --porcelain --untracked-files=all | grep -vE '^\?\? \.codegraph(/|$)' || true)"
    if [ -n "$dirty" ]; then
      echo "FATAL: EverOS 工作树不干净(未提交/未跟踪都会绕过版本钉死),拒绝启动:"; echo "$dirty"; exit 1
    fi
    if ! diff <(_fingerprint) "$PIN_FILE" >&2; then
      echo "FATAL: EverOS 版本指纹 ≠ PIN——先过升级门(runbook ④)再更新 PIN"; exit 1
    fi
    exec "${EVEROS_BIN:?env 缺 EVEROS_BIN}" server start --root "$ROOT"
    ;;
  smoke)
    _search smoke hybrid && echo "search smoke ok" || { echo "search smoke 失败,看实例日志"; exit 1; }
    ;;
  status)
    _search probe keyword && echo up || echo down
    ;;
esac
