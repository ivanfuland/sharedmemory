# sharedmemory

共享记忆层 — 跨 Agent（Claude Code / Codex / OpenClaw）会话记忆系统。
CASS 捕获 → 夜间蒸馏桥 → GBrain 结论库，本地全链路，MCP 统一接入。

- 设计：cc-workspace `docs/projects/shared-memory/specs/`（spec v10 PASS）
- 路线图：Obsidian `[[006-路线图：共享记忆层实施]]`，回归口令「接共享记忆层」
- 本仓：M0 起的契约工件 + 测试 + 后续蒸馏桥/cass-mcp 代码

## 协作规范
**master 受保护**：禁止直接 push，一律走 PR。分支命名 `m0/<task>`、`m1/<...>` 等。

## 目录
- `contracts/` — 受控契约文档（schema + 指纹）
- `tests/` — pytest 契约测试
- `fixtures/` — 合成测试数据（real-snapshots/ 不进 git）
- `scripts/` — 探测与指纹脚本
