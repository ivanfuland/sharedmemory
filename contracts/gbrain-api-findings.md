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

## serve --http 真实鉴权（Task 5 实测）
- OAuth 2.1：`/.well-known/oauth-authorization-server` 暴露 `/authorize` `/token`，PKCE S256
- admin bootstrap token 打到 stdout（64-hex），`/admin` UI 登录
- **无 `gbrain auth` CLI**——scoped client 经 /admin UI 或 `--enable-dcr`（DCR 默认关）
- 核心安全属性已验：`POST /mcp` 无 token / bogus token → **401**（鉴权 HTTP 层强制）
- **细粒度 scope/source 负例（read-only 写拒 / bridge 越权 source 拒）依赖 OAuth client provisioning → 归 M2**（三端 scoped client 实际接入时验）

## 中文 wikilink（Task 5 实测，spec §5 P0 出口④ 通过）
- 中文 slug 页可建；body `[[中文slug]]` 自动抽成 backlink（link_type=mentions, source=markdown）
- 显式 `gbrain link <from> <to>` → link_type=involves, source=manual
- `backlinks <slug> --json` 返回结构化 directional 数组 `[{from_slug,to_slug,link_type,...}]`
- 结论：**中文 wikilink 原生可用，无需拼音 slug 降级**

## 六类契约 M0 落地状态
| 类 | 真实 API 验证 | M0 测试 |
| --- | --- | --- |
| 1 成功 | put(stdin md)+timeline-add+get+timeline | ✓ |
| 2 key 回查 | timeline 文本扫描（search 不索引 timeline） | ✓ |
| 3 幂等 | timeline-add 原生去重 | ✓ |
| 4 冲突 | 两条独立 timeline 并存 | ✓ |
| 5 权限 | serve --http OAuth，HTTP 层鉴权强制 401 | ✓（scope 细粒度→M2） |
| 6 stale | 原生 (stale) 标记可解析 + 干净页控制组 | ✓（同步强制正例→M3 dream） |
