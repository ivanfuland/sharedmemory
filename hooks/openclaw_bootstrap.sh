#!/usr/bin/env bash
# OpenClaw bootstrap adapter — 本地 gbrain CLI 读 digest → 注入 agent AGENTS.md preamble。
#
# Semantic action : 在 OpenClaw agent 启动时注入记忆层相关结论。
# Host mapping   : OpenClaw 无内置 hook 系统（probed 2026-06-23: ~/.openclaw/ 无 hooks.json /
#                  startup hook 字段；openclaw.json 顶层键：meta/env/wizard/auth/models/agents/
#                  tools/bindings/messages/commands/session/channels/gateway/skills/plugins/talk/mcp）。
#                  OpenClaw agent 通过 workspace/AGENTS.md 注入启动上下文（每个 agent 在
#                  ~/.openclaw/agents/<name>/agent/codex-home/AGENTS.md 有专属 AGENTS.md）。
#                  本 adapter 向 agent 的 AGENTS.md 头部 prepend digest 段落（幂等：先清旧注入块）。
#                  目标 agent 由 $OPENCLAW_AGENT 环境变量指定（缺省 main）。
#                  Task6 将接线到 OpenClaw 的 agent 启动流程（例如 `openclaw config set` 注册
#                  startup_script，或 AGENTS.md @import 方式），并按真实机制调整注入路径。
# Fallback       : 任何错误 → 在 stdout 输出一行状态 "[记忆层] <状态>"，不修改任何文件，exit 0。
#
# NOTE: 该 adapter 修改 AGENTS.md 前会备份原文件（.gbrain-digest.bak），Task6 接线验证后可移除备份。

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

# Workspace / agent 名作为 query 主词
AGENT="${OPENCLAW_AGENT:-main}"
WS="$(basename "${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}" 2>/dev/null)" 2>/dev/null || WS="workspace"
WS="${WS//[^[:alnum:]_-]/}"
WS="${WS:-workspace}"

DIGEST_JSON="$(
    python3 "$ROOT/hooks/gbrain_digest.py" "$WS $AGENT 相关结论 决策 偏好" 2>/dev/null
)" || DIGEST_JSON=""

# 注入到 agent 的 codex-home/AGENTS.md（fail-soft）
python3 - "$DIGEST_JSON" "$AGENT" <<'PY'
import json, os, sys

try:
    raw = sys.argv[1] if len(sys.argv) > 1 else ""
    d = json.loads(raw) if raw.strip() else {}
except Exception:
    d = {}

agent = sys.argv[2] if len(sys.argv) > 2 else "main"
ctx = d.get("context") or ""
status = d.get("status") or "记忆层：不可用（hook 兜底）"
content = ctx if ctx else f"[记忆层] {status}"

INJECT_BEGIN = "<!-- gbrain-digest:begin -->"
INJECT_END   = "<!-- gbrain-digest:end -->"
inject_block = f"{INJECT_BEGIN}\n{content}\n{INJECT_END}\n\n"

agents_md = os.path.expanduser(
    f"~/.openclaw/agents/{agent}/agent/codex-home/AGENTS.md"
)

try:
    if os.path.isfile(agents_md):
        original = open(agents_md, encoding="utf-8").read()
        # 幂等：清除旧注入块
        import re
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
except Exception:
    # 写入失败不崩：仅输出 stdout
    pass

# stdout 输出（供调试 / 管道注入）
print(content)
PY

exit 0
