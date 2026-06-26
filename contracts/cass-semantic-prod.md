# CASS 生产语义检索契约（M4 cass-mcp 消费的单一事实源）

> 架构：**单版本 fork 独占**（plan v2，2026-06-27 上线）。fork `cass-infinity` 0.6.17 独占 canonical DB，
> 老 baseline 0.6.13 退役。详见 `cc-workspace/docs/projects/shared-memory/plans/2026-06-26-cass-semantic-production.md`。

## 接口

- **binary**：`~/.local/bin/cass-infinity`（原生，**非容器**；`cass` 已 symlink 到它）。来源 = fork `scripts/setup-cass-fork.sh`。
- **data_dir（canonical）**：`~/.local/share/coding-agent-search`（fork 独占，读+写+索引）。
- **语义检索命令**：
  ```bash
  CASS_DATA_DIR=~/.local/share/coding-agent-search CASS_INFINITY_URL=http://127.0.0.1:7997 \
    cass-infinity search "<query>" --mode semantic --daemon --model bge-m3 [--rerank] --json --limit <K>
  ```
  词法检索：`--mode lexical`。
- **依赖**：
  - `cc-infinity.service`（systemd user service，常驻，GPU，bge-m3+bge-reranker-v2-m3 @ 127.0.0.1:7997，digest pin）—— **必须 active**。
  - canonical 由 `cass-index-daily`（Inngest cron，见 Phase C）**增量维护**（pull 新会话 + 增量嵌入）。

## 就绪契约（cass-mcp 查询前应校验）

- **语义就绪**：`current/vector_index/semantic_manifest.json` 的 `quality_tier.ready == true` 且 `embedder_id == "bge-m3"`。
- **词法就绪**：`index/` 目录存在可打开。
- **Infinity 就绪**：`GET http://127.0.0.1:7997/health` 200。

## 新鲜度

= **pull cadence**（默认每日 Inngest cron）。新对话在下次 cron 后进库可搜。增量机制（scan watermark + tail
+ external_id 去重 + embedding memoization）保证 pull 便宜，不全量重嵌。

## 生产实测（2026-06-27 首建）

- 语料：**2862 conv / 82036 msg**，全 5 provider（claude_code 2104 / codex 277 / openclaw 452 / pi 29 / gemini 2）。
- 语义索引：bge-m3，**81975 docs，ready True**，fp `content-v1:2862:2862:82036`。
- 召回门：**semantic relevance@5 = 0.62**（floor 0.55）/ lexical = 1.00。PASS。

## 隔离 / 安全

- fork 独占 canonical；老 baseline 0.6.13 二进制退役（`~/.local/bin/cass.0.6.13.bak`），老库备份
  `~/.local/share/coding-agent-search.0.6.13.bak.20260627`（+ `.preswap.*`）。回滚 = 停用 fork + 还原备份 + 还原 `cass` 二进制。
- **禁**：用 0.6.13 老二进制打开新 0.6.17 canonical（schema 不兼容，会迁移/损坏）。

## 给 M4 cass-mcp 的修订点

- `CASS_BIN` = `cass-infinity`；`CASS_DATA_DIR` = `~/.local/share/coding-agent-search`（canonical）。
- search 加语义 flags：`--mode semantic --daemon --model bge-m3`（可选 `--rerank` 提精度，spike 实测 rerank@5≈0.97）。
- lookup skill：砍掉「关键词扩展绕法」（语义直接召回）；知「新鲜度 = pull cadence，最新对话可能未入索引」。
- 查询前跑就绪契约（语义+词法+Infinity 三就绪）。
