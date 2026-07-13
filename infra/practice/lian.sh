#!/usr/bin/env bash
# lian.sh — gbrain 手动"炼记忆"练习台(常驻)
# ---------------------------------------------------------------------------
# 一条命令手动跑 dream(反思/主题合成),自动:
#   1) 用独立常驻 worktree 的**新代码**(gbrain-deepseek origin/master, 含 patterns 修复 PR #5)
#      —— 不碰生产 clone ~/projects/gbrain-deepseek(旧代码)。
#   2) source 生产网关 env(config.env + pg-memory/.env)+ 指向生产脑 GBRAIN_HOME。
#   3) 起后台 worker 消费 dream 投的 subagent job,脚本退出时自动 kill。
#   4) 把你传的参数原样转给 `gbrain dream`。
#
# 用法:
#   炼反思(synthesize 单条 transcript, 会花钱):
#     bash lian.sh --input <transcript文件> --json
#   炼主题(patterns 跨反思找主题, 会花钱):
#     bash lian.sh --phase patterns --json
#   验证台子(零花费, patterns dryRun 在投 job 前 return,不调 LLM):
#     bash lian.sh --phase patterns --dry-run --json
#
# 注意:
#   * 裸跑 `bash lian.sh`(无参数)= 全 dream cycle = 含 synthesize 会花钱 → 本脚本拒绝,必须给明确参数。
#   * `--dry-run` 只有 patterns 相位是零 LLM;synthesize 的 dry-run 仍跑 Haiku 显著性闸(会花小钱),
#     故零花费验证只用 `--phase patterns --dry-run`。
#   * 拿一条会话的 transcript 喂 --input:见同目录说明或 /tmp/cc-practice-harness.md「导出」节。
#   * worker 日志:/tmp/cc-lian-worker.log
# ---------------------------------------------------------------------------
set -euo pipefail

WORKTREE="$HOME/projects/gbrain-deepseek.worktrees/practice"
SM="$HOME/projects/sharedmemory"
BUN="$HOME/.bun/bin/bun"
WORKER_LOG="/tmp/cc-lian-worker.log"

usage() {
  sed -n '2,24p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'
}

if [ "$#" -eq 0 ]; then
  usage
  echo
  echo "错误:未传参数。给 dream 传明确子命令(见上用法)。" >&2
  exit 2
fi

# --- 前置检查 ---
[ -f "$WORKTREE/src/cli.ts" ] || { echo "FATAL: 练习 worktree 缺失或未 bun install: $WORKTREE" >&2; exit 1; }
[ -x "$BUN" ] || { echo "FATAL: 找不到 bun: $BUN" >&2; exit 1; }
[ -f "$SM/infra/gbrain/config.env" ] || { echo "FATAL: 缺 config.env" >&2; exit 1; }

# --- env(照抄 backup-brain.sh / install-service.sh 的 source 语义) ---
export PATH="$HOME/.bun/bin:$PATH"
export GBRAIN_HOME="$SM/sandbox/gbrain-pg"
set -a
source "$SM/infra/gbrain/config.env"
[ -f "$SM/infra/pg-memory/.env" ] && source "$SM/infra/pg-memory/.env"
set +a

# --- 起后台 worker(消费 patterns/synthesize 投的 subagent job) ---
: > "$WORKER_LOG"
"$BUN" "$WORKTREE/src/cli.ts" jobs work >>"$WORKER_LOG" 2>&1 &
WORKER_PID=$!
trap 'kill "$WORKER_PID" 2>/dev/null || true' EXIT
echo "[lian] worker 已起 pid=$WORKER_PID  log=$WORKER_LOG"

# 给 worker 一点时间连 DB;不存活则告警(dry-run 不受影响,真跑会卡在 poll)
sleep 2
if ! kill -0 "$WORKER_PID" 2>/dev/null; then
  echo "[lian] 警告:worker 未存活,查看 $WORKER_LOG" >&2
fi

# --- 跑 dream(参数原样透传);set -e 保证退出码=dream 退出码,trap 仍会 kill worker ---
echo "[lian] gbrain dream $*"
"$BUN" "$WORKTREE/src/cli.ts" dream "$@"
