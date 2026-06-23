#!/usr/bin/env bash
# CC SessionStart hook — 本地 gbrain CLI 读 digest → additionalContext 注入。
#
# Semantic action : 在 Claude Code 会话启动时注入记忆层相关结论。
# Host mapping   : CC SessionStart hook 读 stdin（JSON payload），输出：
#                    {"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"..."}}
#                  (probed 2026-06-23: ~/.claude/settings.json hooks={} — 无现有 hook；
#                   契约来自 CC 官方文档 hooks#SessionStart。Task6 接线入 settings.json。)
# Fallback       : 任何错误（gbrain 不可达/Python 崩/env 畸形）→ additionalContext 注一行
#                  "[记忆层] <状态>" 状态行，永远 exit 0 — 绝不崩 hook。
#
# 关键：export GBRAIN_HOME="${GBRAIN_HOME:-...}" 尊重预设值，不硬覆盖。
# 这样 fail-soft 测试注入 GBRAIN_HOME=/nonexistent 才能真正命中不可达分支 (codex R2 #12)。

set +e  # 全程不因子命令失败中断

ROOT="$HOME/projects/sharedmemory"
export GBRAIN_HOME="${GBRAIN_HOME:-$ROOT/sandbox/gbrain-pg}"
export PATH="$HOME/.bun/bin:$PATH"

# 加载 gbrain 环境变量（缺失时静默忽略）
set -a
# shellcheck source=/dev/null
source "$ROOT/infra/gbrain/config.env" 2>/dev/null || true
# shellcheck source=/dev/null
source "$ROOT/infra/pg-memory/.env" 2>/dev/null || true
set +a

# 从 CLAUDE_PROJECT_DIR（CC 传入当前项目路径）提取 workspace 名作为 query 主词
WS="$(basename "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null)" 2>/dev/null || WS="workspace"
# 对畸形 env（含 shell 元字符）做安全提取：只取非特殊字符部分
WS="${WS//[^[:alnum:]_-]/}"
WS="${WS:-workspace}"

# 调 Python digest builder（Python 自身崩也不影响 exit 0，输出为空）
DIGEST_JSON="$(
    python3 "$ROOT/hooks/gbrain_digest.py" "$WS 相关结论 决策 偏好" 2>/dev/null
)" || DIGEST_JSON=""

# 输出符合 CC SessionStart 契约的 JSON
# §2.8：注空时也注入「一行状态」可见可审计，不 suppressOutput (codex R1 #8)
python3 - "$DIGEST_JSON" <<'PY'
import json, sys

try:
    raw = sys.argv[1] if len(sys.argv) > 1 else ""
    d = json.loads(raw) if raw.strip() else {}
except Exception:
    d = {}

ctx = d.get("context") or ""
status = d.get("status") or "记忆层：不可用（hook 兜底）"
# 注入内容：有命中用 context，否则用状态行（非空，可审计）
inject = ctx if ctx else f"[记忆层] {status}"

print(json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": inject,
        }
    },
    ensure_ascii=False,
))
PY

exit 0
