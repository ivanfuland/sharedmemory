# hooks/ — SessionStart 自动 digest 注入

M2 Task3 产物：共享 builder + 三端 adapter，hub 本地 gbrain CLI 读路径。

## 文件清单

| 文件 | 作用 |
|------|------|
| `gbrain_digest.py` | 共享 builder：`_run_query` → `parse_query` → `build_digest_from_raw`，fail-soft 硬要求 |
| `cc_sessionstart.sh` | CC SessionStart hook adapter |
| `codex_sessionstart.sh` | Codex session 启动 adapter |
| `openclaw_bootstrap.sh` | OpenClaw agent 启动 adapter |

---

## 三端 adapter 设计

### CC — `cc_sessionstart.sh`

**语义动作**：在 Claude Code 会话启动时注入记忆层相关结论到 `additionalContext`。

**宿主契约**（probed 2026-06-23）：
- `~/.claude/settings.json` 中 `hooks` 字段当前为空 `{}`，无现有 SessionStart hook。
- CC SessionStart hook 输出格式为：
  ```json
  {
    "hookSpecificOutput": {
      "hookEventName": "SessionStart",
      "additionalContext": "<注入内容>"
    }
  }
  ```
- CC 从 stdin 读取 JSON payload（触发时），adapter stdout 需符合上述格式。

**Task6 接线位置**：`~/.claude/settings.json` → `hooks.SessionStart` 数组，元素类型 `command`：
```json
{
  "hooks": {
    "SessionStart": [
      {
        "type": "command",
        "command": "bash /path/to/hooks/cc_sessionstart.sh"
      }
    ]
  }
}
```

**回滚**：从 `settings.json` 删除该 SessionStart 条目即可。

---

### Codex — `codex_sessionstart.sh`

**语义动作**：在 Codex 会话启动时注入记忆层相关结论。

**宿主契约**（probed 2026-06-23，Task6 实证修正）：
- `~/.codex/hooks.json` **不存在**，Codex 无内置 hook 系统。
- **`~/.codex/memories/` 不会自动加载**（Task6 投资证实测）；Codex 在会话启动时自动加载的是 **`~/.codex/AGENTS.md`**（`user_instructions`，经 AgentsMdManager）。
- 故 adapter **向 `~/.codex/AGENTS.md` 顶部 prepend** `<!-- gbrain-digest:begin -->`…`<!-- gbrain-digest:end -->` 包裹的 digest 块（幂等，首次备份 `.bak`，同 OpenClaw 方案）。目标文件可由 `CODEX_AGENTS_FILE` env 覆盖（测试用）。
- 输出由 Codex 运行时作为 user_instructions 拼入；只在会话启动那一刻读取，内容靠定时刷新（非每会话实时查询）。

**Task6 接线位置（live 激活延后，见 `contracts/M2-EXIT.md` 激活清单）**：
1. 由刷新机制（如每日 timer）跑 `codex_sessionstart.sh` 把最新 digest prepend 进 `~/.codex/AGENTS.md`。
2. Codex 下次会话启动自动读取该文件。

**回滚**：删除 `~/.codex/AGENTS.md` 中 `<!-- gbrain-digest:begin -->` 到 `<!-- gbrain-digest:end -->` 的块，或用 `.bak` 恢复。

---

### OpenClaw — `openclaw_bootstrap.sh`

**语义动作**：在 OpenClaw agent 启动时注入记忆层相关结论到 agent AGENTS.md。

**宿主契约**（probed 2026-06-23）：
- `~/.openclaw/` 无 hooks 系统（`openclaw.json` 顶层键无 hooks/startup/bootstrap 字段）。
- OpenClaw agent 启动上下文来源：`~/.openclaw/agents/<name>/agent/codex-home/AGENTS.md`。
- Adapter 向该文件头部 prepend `<!-- gbrain-digest:begin/end -->` 包裹的 digest 块（幂等，支持重跑）。
- 首次运行时备份原始文件为 `AGENTS.md.gbrain-digest.bak`。
- 目标 agent 由 `$OPENCLAW_AGENT` 环境变量指定（默认 `main`）。

**Task6 接线位置**：
1. 验证 OpenClaw 是否有 startup_script / plugin hook 可以在 agent 启动前执行本 adapter。
2. 若无，考虑通过 `openclaw config set` 注册 per-agent startup command（若版本支持）。
3. 或在 agent 专属 AGENTS.md 中添加 `@import <gbrain-digest.md>` 方式（若 OpenClaw 支持）。

**回滚**：用备份恢复 `cp AGENTS.md.gbrain-digest.bak AGENTS.md`，或手动删除 `<!-- gbrain-digest:begin -->` 到 `<!-- gbrain-digest:end -->` 的块。

---

## fail-soft 核心要求

所有 adapter 遵循以下硬要求（§2.8 + codex R1 #8/#12）：

1. **永远 `exit 0`**：任何错误不崩 hook（gbrain 不可达、Python 异常、畸形 env）。
2. **注空也注入状态行**：无命中时不 suppress，注入 `[记忆层] <状态>` 可见状态行，保持可审计。
3. **CC adapter `GBRAIN_HOME` 不硬覆盖**：使用 `"${GBRAIN_HOME:-默认值}"` 尊重预设（fail-soft 测试注入坏值才能真正命中不可达分支）。
4. **阈值保守默认**：`DEFAULT_THRESHOLD=0.75`，宁高勿低（漏注优于污染）；`config/m2-thresholds.json` 可覆盖（Task4 产）。
5. **注入硬上限 ≤1500 tokens**（粗算字符数上限 `max_tokens * 4`）。

## 测试

```bash
# 6 builder 单元 + 1 adapter 端到端 fail-soft 测试
GBRAIN_HOME="$PWD/sandbox/gbrain-pg" uv run pytest tests/test_m2_digest.py -v
```

## 接线状态

| adapter | 已写 | 已测 | Task6 接线 |
|---------|------|------|------------|
| CC | ✅ | ✅ | 待定 |
| Codex | ✅ | ✅ | 待定 |
| OpenClaw | ✅ | ✅ | 待定 |
