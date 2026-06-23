#!/usr/bin/env bash
# Codex SessionStart adapter — 本地 gbrain CLI 读 digest → prepend 到 AGENTS.md。
#
# Semantic action : 在 Codex 会话启动时注入记忆层相关结论。
# Host mapping   : Codex 会话启动上下文通过 ~/.codex/AGENTS.md 注入（probed 2026-06-23：
#                  Codex 自动将 ~/.codex/AGENTS.md 作为 user_instructions 加载）。
#                  本 adapter 向 AGENTS.md 头部 prepend digest 段落（幂等：
#                  先清旧注入块，再 prepend 新块）。
#                  目标文件默认 ~/.codex/AGENTS.md，可由 $CODEX_AGENTS_FILE 覆盖
#                  （测试时指向 temp 文件，生产时缺省不变）。
# Fallback       : 任何错误 → 在 stdout 输出一行状态 "[记忆层] <状态>"，不修改任何文件，exit 0。
#
# 关键：export GBRAIN_HOME="${GBRAIN_HOME:-...}" 尊重预设值，不硬覆盖。
# 这样 fail-soft 测试注入 GBRAIN_HOME=/nonexistent 才能真正命中不可达分支。

set +e

ROOT="$HOME/projects/sharedmemory"
export GBRAIN_HOME="${GBRAIN_HOME:-$ROOT/sandbox/gbrain-pg}"
export PATH="$HOME/.bun/bin:$PATH"

set -a
# shellcheck source=/dev/null
source "$ROOT/infra/gbrain/config.env" 2>/dev/null || true
# shellcheck source=/dev/null
source "$ROOT/infra/pg-memory/.env" 2>/dev/null || true
set +a

# Workspace 名作为 query 主词（Codex 通常在项目目录下启动）
WS="$(basename "${CODEX_PROJECT_DIR:-${PWD}}" 2>/dev/null)" 2>/dev/null || WS="workspace"
WS="${WS//[^[:alnum:]_-]/}"
WS="${WS:-workspace}"

DIGEST_JSON="$(
    python3 "$ROOT/hooks/gbrain_digest.py" "$WS 相关结论 决策 偏好" 2>/dev/null
)" || DIGEST_JSON=""

# 注入到 AGENTS.md（fail-soft）
# 目标文件：$CODEX_AGENTS_FILE（测试覆盖） 或默认 ~/.codex/AGENTS.md
python3 - "$DIGEST_JSON" "${CODEX_AGENTS_FILE:-}" <<'PY'
import json, os, re, sys

try:
    raw = sys.argv[1] if len(sys.argv) > 1 else ""
    d = json.loads(raw) if raw.strip() else {}
except Exception:
    d = {}

# 目标文件：argv[2] 覆盖（测试用）或默认 ~/.codex/AGENTS.md
agents_file_override = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else ""
agents_md = agents_file_override if agents_file_override else os.path.expanduser("~/.codex/AGENTS.md")

ctx = d.get("context") or ""
status = d.get("status") or "记忆层：不可用（hook 兜底）"
content = ctx if ctx else f"[记忆层] {status}"

INJECT_BEGIN = "<!-- gbrain-digest:begin -->"
INJECT_END   = "<!-- gbrain-digest:end -->"
inject_block = f"{INJECT_BEGIN}\n{content}\n{INJECT_END}\n\n"

try:
    if os.path.isfile(agents_md):
        original = open(agents_md, encoding="utf-8").read()
        # 幂等：清除旧注入块
        cleaned = re.sub(
            rf"{re.escape(INJECT_BEGIN)}.*?{re.escape(INJECT_END)}\n*",
            "",
            original,
            flags=re.DOTALL,
        )
        # 备份原始内容（幂等：仅在 bak 不存在时写）
        bak = agents_md + ".gbrain-digest.bak"
        if not os.path.isfile(bak):
            open(bak, "w", encoding="utf-8").write(original)
        # Prepend 注入块
        open(agents_md, "w", encoding="utf-8").write(inject_block + cleaned)
    else:
        # 文件不存在时，仅写注入块（首次初始化）
        os.makedirs(os.path.dirname(agents_md) if os.path.dirname(agents_md) else ".", exist_ok=True)
        open(agents_md, "w", encoding="utf-8").write(inject_block)
except Exception:
    # 写入失败不崩：仅输出 stdout
    pass

# stdout 也输出（供调试 / 管道注入）
print(content)
PY

exit 0
