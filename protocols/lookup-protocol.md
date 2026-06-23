# Lookup Protocol（记忆层检索协议）

会话中需要「过往结论/事实/决策」时，按序检索，**首中即停**：

1. `gbrain search <kw>`（关键词，tsvector 全文）
2. `gbrain query <问题>`（语义混合检索）
3. `gbrain get <slug>`（已知页直读）
4. CASS（know-how / "之前/上次怎么做的"）——**"之前/上次"类必查 CASS**

消费 compiled truth 前**检查 stale 标记**：页标 `(stale)` 或有 `contradicts-truth` 条目 →
以最新 timeline 证据为准，不照搬旧 truth（§2.6 live 路径协议防线）。

## 检索优先级说明

| 步骤 | 工具 | 适用场景 | 命中条件 |
|------|------|----------|----------|
| 1 | `gbrain search` | 关键词明确，已知术语 | 精确词命中，tsvector 匹配 |
| 2 | `gbrain query` | 问题模糊，语义近似 | 向量相似度 ≥ 阈值 |
| 3 | `gbrain get` | 已知 slug / 页面直读 | slug 存在 |
| 4 | CASS | 操作型 know-how，流程记忆 | "之前/上次" 场景必走此步 |

## Stale 检查规则

- 检索到的页面带有 `(stale)` 标签 → **不直接引用**，优先查 timeline 事件流获取最新事实
- 页面包含 `contradicts-truth` 字段 → 读取该字段指向的最新覆盖记录
- 多页命中且互相矛盾 → 以时间戳最新的 timeline 条目为准
