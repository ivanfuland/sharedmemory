# GBrain 0.42.37 真实 API 探测（M0 Task 4，校正 plan 假设）

> plan 的 gbrain 命令/输出假设来自 deepwiki 调研，与实装 CLI 有差异。
> 这正是 M0 的价值：在建蒸馏桥（M3）前用真工具校正。

## 真实命令映射（替换 plan 的 put-page/add-timeline-entry 等假设）
| 语义 | 真实命令 | 备注 |
| --- | --- | --- |
| 建/改页 | `gbrain put <slug>`（markdown 走 **stdin**） | 非 `put-page`；无 --json 写参，整页 markdown |
| 读页 | `gbrain get <slug>` | **返回 markdown 文本（frontmatter+body），`--json` 被忽略** |
| 加 timeline | `gbrain timeline-add <slug> <date> <text>`（**位置参数**） | 非 `--date/--source/--text`；**无 --source/--flag** |
| 看 timeline | `gbrain timeline <slug>` | 文本行 `<ISO时间>  <text>` |
| 关键词搜 | `gbrain search <query>` | tsvector；**只索引页 body，不索引 timeline 文本** |
| 混合查询 | `gbrain query <q>` | 需嵌入（M1） |
| 连边 | `gbrain link <from> <to> [--link-type T]` / `gbrain backlinks <slug>` | backlinks `--json` 返回数组 |
| 维护 | `gbrain dream` | 过夜巩固 |
| MCP | `gbrain serve` (stdio) / `gbrain serve --http [--port N]`（OAuth 2.1） | class5 用 |

## 探测结论：多个 plan/spec 担心点被**原生解决**（重大 de-risk）
1. **中文 slug 页可用**：`projects/共享记忆层`、`people/test-zhang` 均建成 → wikilink 门的"可能要降级拼音 slug"担心**不成立**，中文 slug 原生 OK（spec §2.5.2 的拼音 fallback 暂不需要）
2. **CJK body 搜索可用**：`search "雷火"` 命中（[1.37]）→ 中文词法检索 baseline 比预期好（v0.42 CJK fallback 生效）
3. **timeline 原生去重**：同 `<date> <text>` 两次 → timeline 只 1 条（`native_dedup=true`）→ 蒸馏桥 reconcile 部分免费
4. **原生 stale 检测**：search 输出对 compiled-truth 落后的页直接标 `(stale)` → 第6类 stale 信号**原生存在**，不需自造 flag

## 影响蒸馏桥（M3）设计的差异
- **写入 = put 整页 markdown + timeline-add**，不是 plan 假设的细粒度 JSON 写 API。compiled-truth/timeline 是 markdown 页内结构 + timeline-add 子命令
- **idempotency key 不能靠 search 回查**（timeline 文本不进搜索索引）→ 改用 `gbrain timeline <slug>` 文本扫描查重（且 timeline-add 本身去重，双保险）
- **get 返回 markdown 文本**，蒸馏桥/契约测试须解析 markdown（frontmatter + body + 配合 `timeline` 子命令），非 JSON dict
- **timeline-add 无 source 字段** → provenance（来源会话）须编码进 text 或用 page frontmatter，bridge service client 的 source 隔离要靠 serve --http 的 OAuth scope（class5）

## 契约测试调整方向
plan 的 6 类按真实 API 重写：put(stdin md)/get(md)/timeline-add(位置参)/timeline/search(body)/backlinks/serve--http(OAuth)。
stale 用原生 `(stale)` 标记；dedup 用 timeline 原生去重 + timeline 文本扫描；class5 走 serve --http OAuth。
