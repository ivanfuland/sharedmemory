#!/usr/bin/env bash
# everos-prod 常驻实例生命周期(照 eval_instance_m1c.sh 模式)。用法: {setup|pin|run|smoke|status}
# 常驻由 systemd user unit(everos-prod.service)以 `run` 启动;`run` 校验版本 PIN 后 exec。
# 铁律: 拓扑一律私有 env 注入(PUBLIC 仓零字面量);admin key 绝不进实例 env;
#       版本钉死(spec §3):EverOS 跟随 mutable checkout/venv 漂移必须被启动挡住,
#       PIN 只能经升级门(校准集回归过了)更新——`pin` 子命令写,`run` 子命令验。
set -euo pipefail
ENVSH="${EVEROS_PROD_ENV:?set EVEROS_PROD_ENV to your private env file}"
# set -a:source 出来的变量必须 export(codex PR58-P0)——systemd 只传 EVEROS_PROD_ENV 一个变量,
# `run` 的 exec everos 子进程要靠这里导出的 EVEROS_API__*/EVEROS_LLM__* 等才能按生产配置起服。
set -a
# shellcheck disable=SC1090
source "$ENVSH"
set +a
ROOT="${EVEROS_PROD_ROOT:?env 缺 EVEROS_PROD_ROOT}"
PORT="${EVEROS_PROD_PORT:?env 缺 EVEROS_PROD_PORT}"
TEMPLATE="${EVEROS_TEMPLATE_DIR:?env 缺 EVEROS_TEMPLATE_DIR}"
AGENT="${EVEROS_FEED_AGENT_ID:-everos-prod}"
BASE="$(dirname "$ROOT")"
PIN_FILE="$BASE/PIN"

_fingerprint() {  # 两因子:源码 git SHA + venv 依赖冻结哈希(抓 git 不变但 pip install 变的漂移)
  # fail-loud(T5 评审 Important-2):venv python 缺失 / pip freeze 失败或为空时,若静默成空串,
  # pin 与 run 两侧会算出同一个"空 freeze"哈希 → 假匹配,fail-closed 反转成 fail-open。
  SRC="${EVEROS_SRC_DIR:?env 缺 EVEROS_SRC_DIR}"
  BIN="${EVEROS_BIN:?env 缺 EVEROS_BIN}"
  PY="$(dirname "$BIN")/python"
  [ -x "$PY" ] || { echo "FATAL: venv python missing: $PY" >&2; return 1; }
  freeze="$("$PY" -m pip freeze 2>/dev/null)" || { echo "FATAL: pip freeze failed" >&2; return 1; }
  [ -n "$freeze" ] || { echo "FATAL: pip freeze empty" >&2; return 1; }
  sha="$(git -C "$SRC" rev-parse HEAD)" || { echo "FATAL: git rev-parse failed in $SRC" >&2; return 1; }
  echo "git_sha=$sha"
  echo "venv_freeze_sha256=$(printf '%s' "$freeze" | sha256sum | cut -d' ' -f1)"
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
    # tmp+mv:_fingerprint 半途失败不得留下空/半写 PIN(空 PIN 会让 run 侧永远 mismatch 还难诊断)。
    # 先经命令替换拿到完整输出再落盘——直接 `_fingerprint > tmp` 会在函数执行前就先
    # truncate/创建 tmp 文件,函数早退失败时也会留下一个空 .tmp 残留。
    fp="$(_fingerprint)" || { echo "FATAL: fingerprint failed, PIN not written" >&2; exit 1; }
    printf '%s\n' "$fp" > "$PIN_FILE.tmp"
    mv "$PIN_FILE.tmp" "$PIN_FILE"
    echo "pinned:"; cat "$PIN_FILE"
    ;;
  run)  # systemd 入口:PIN 校验 fail-closed,不匹配拒绝启动(防绕过校准集回归门的静默漂移)
    [ -s "$PIN_FILE" ] || { echo "FATAL: $PIN_FILE 不存在或为空——先跑 pin(首次)或升级门"; exit 1; }
    SRC="${EVEROS_SRC_DIR:?env 缺 EVEROS_SRC_DIR}"
    # 含未跟踪文件(R2-P1-2):editable install 下未跟踪的新模块 git SHA 和 pip freeze 都抓不到,
    # 却直接可 import——只 allowlist 已知非代码产物 .codegraph/。
    # 先拿 git 输出再过滤(终审 Minor-2):git 自身失败不许被 `|| true` 吞掉。
    porcelain="$(git -C "$SRC" status --porcelain --untracked-files=all)" || { echo "FATAL: git status failed in $SRC" >&2; exit 1; }
    dirty="$(printf '%s\n' "$porcelain" | grep -vE '^\?\? \.codegraph(/|$)' || true)"
    dirty="${dirty#$'\n'}"   # 空 porcelain 时 printf 产生的孤行清掉
    if [ -n "$dirty" ]; then
      echo "FATAL: EverOS 工作树不干净(未提交/未跟踪都会绕过版本钉死),拒绝启动:"; echo "$dirty"; exit 1
    fi
    fp="$(_fingerprint)" || { echo "FATAL: fingerprint failed at run time"; exit 1; }
    if ! diff <(printf '%s\n' "$fp") "$PIN_FILE" >&2; then
      echo "FATAL: EverOS 版本指纹 ≠ PIN——先过升级门(runbook ④)再更新 PIN"; exit 1
    fi
    exec "${EVEROS_BIN:?env 缺 EVEROS_BIN}" server start --root "$ROOT"
    ;;
  smoke)
    if _search smoke hybrid; then
      echo "search smoke ok"
    else
      echo "search smoke 失败,看实例日志"; exit 1
    fi
    ;;
  status)
    # down 时 exit 1(codex PR58-P2):自动化按退出码判活,down 不许 false-green
    if _search probe keyword; then echo up; else echo down; exit 1; fi
    ;;
  *)
    echo "usage: everos_prod_instance.sh {setup|pin|run|smoke|status}" >&2; exit 2
    ;;
esac
