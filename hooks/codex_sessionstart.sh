#!/usr/bin/env bash
# Codex SessionStart adapter — 本地 gbrain CLI 读 digest → 写入 context 文件供 Codex 启动读取。
#
# Semantic action : 在 Codex 会话启动时注入记忆层相关结论。
# Host mapping   : Codex 无 hooks.json（probed 2026-06-23: ~/.codex/ 中不存在 hooks.json）。
#                  Codex 的 session 启动上下文通过 AGENTS.md / 项目级 context 文件注入。
#                  本 adapter 将 digest 写入 ~/.codex/memories/gbrain-digest.md（Codex 启动时
#                  自动加载 ~/.codex/memories/ 下的文件作为 system context 补充）。
#                  若 Codex 版本不支持 memories/ 自动注入，Task6 将改为 AGENTS.md @include 方式。
#                  输出格式：Markdown 段落（不是 JSON），由 Codex 运行时作为 context 拼入。
# Fallback       : 任何错误 → 写入一行状态 "[记忆层] <状态>" 到目标文件，exit 0。
#                  Codex memories/ 目录不存在时，仅输出到 stdout，不崩 hook。
#
# NOTE: Task6 将验证 Codex 真实 memories/ 注入机制并按需调整路径/格式。

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

# 将 digest 转为 Markdown 并写入 Codex memories 目录（fail-soft）
python3 - "$DIGEST_JSON" <<'PY'
import json, os, sys

try:
    raw = sys.argv[1] if len(sys.argv) > 1 else ""
    d = json.loads(raw) if raw.strip() else {}
except Exception:
    d = {}

ctx = d.get("context") or ""
status = d.get("status") or "记忆层：不可用（hook 兜底）"
content = ctx if ctx else f"[记忆层] {status}"

# 写入 ~/.codex/memories/gbrain-digest.md（Codex 启动时读取）
memories_dir = os.path.expanduser("~/.codex/memories")
dest = os.path.join(memories_dir, "gbrain-digest.md")

try:
    if os.path.isdir(memories_dir):
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content + "\n")
except Exception as e:
    # 写入失败不崩：仅输出到 stdout 作为调试信息
    pass

# stdout 也输出（供 Task6 管道注入 / 调试）
print(content)
PY

exit 0
