# CASS 语义栈生产部署 — EXIT（v2 单版本）

> 第一行 framing：**Phase 0/A/B 已上线并验证；Phase C（Inngest cron）PREPARED 待协调部署到 jarvis；
> Phase D 文档完成。** 「全吃」全新重摄入 + 召回门 + canonical swap 已完成。

## 1. 重摄入范围 + 数量 + 墙钟
- 范围：全吃（全 5 provider）。
- 结果：**2862 conv / 82036 msg**（claude_code 2104 / codex 277 / openclaw 452 / pi 29 / gemini 2）。> 老 baseline 2670（+192 新会话）。
- 墙钟：词法+DB ingest ~30s（~160 conv/s）；语义 backfill **~42min**（00:25→01:07，81975 docs published）。
- **澄清**：onboarding "19166 sessions" 是膨胀文件计数（含非会话 jsonl，如 `~/.openclaw/workspace/tmp/*.jsonl` 无 session 结构）；2862 conv 是完整真会话集，baseline 并非严重陈旧。

## 2. 召回门
- semantic relevance@5 = **0.62**（floor 0.55）/ lexical = 1.00。**PASS**（新库 + swap 后 canonical 双验）。

## 3. Infinity 服务 + 增量 cron 运维
- `cc-infinity.service`（systemd user，digest pin，127.0.0.1:7997，双模型，重启存活，开机自启 linger）。
- 增量 cron `cass-index-daily`（**PREPARED**，待部署 jarvis；见 `infra/cass-semantic/inngest-cass-index.md`）。entrypoint `index-pull.sh` 就绪。
- **踩坑**：整合 `cass-infinity index --semantic` 触发 phase-2 stall-abort（upstream #244/#258：语义阶段不推进 current 计数，stall 检测器误判 exit 70）。**解**：词法/语义分两步——词法走 `index --full`，语义走 `models backfill` 循环（resilient）。`full-reingest.sh` 已固化此分法。

## 4. 老物处置 + 回滚
- 老二进制：`~/.local/bin/cass.0.6.13.bak`；`cass` → symlink `cass-infinity`。
- 老库备份：`~/.local/share/coding-agent-search.0.6.13.bak.20260627`（+ `.preswap.010901`，可删其一省 2.2G）。
- v1 快照废弃物：`~/.local/share/cass-infinity-semantic.v1-trash-20260627`（可删）。
- **回滚**：停 fork 使用 → `mv coding-agent-search coding-agent-search.0.6.17.bad; mv coding-agent-search.0.6.13.bak.20260627 coding-agent-search` → 还原 `cass` 二进制（`mv cass.0.6.13.bak cass`）。

## 5. M3 蒸馏桥重接清单（M3 激活前必做，现 M3 在 `m3/distill-bridge-cron` 分支推进）
- `CASS_CANON_DB` **路径不变**（2b 占规范路径，老默认值仍解析 `~/.local/share/coding-agent-search/agent_search.db`）。
- **重生成** `cass-canonical.fingerprint`（对 0.6.17 新库 schema DDL；老指纹是 0.6.13，必漂）。
- 更新 `cass-canonical.md` / `M1-EXIT.md` / `M3-EXIT.md` 版本引用：0.6.13 baseline → **0.6.17 fork 独占**。
- M3 激活前对新 canonical 跑一次 `cass_reader.verify_fingerprint` 对齐（additive drift 良性则放行）。

## 6. 移交 M4 读侧修订清单
- cass-mcp：`CASS_BIN`=cass-infinity、`CASS_DATA_DIR`=canonical、search 加 `--mode semantic --daemon --model bge-m3 [--rerank]`。
- lookup skill：砍关键词扩展绕法 + 知 pull cadence 滞后 + 查询前跑三就绪校验。
- 契约单一事实源：`contracts/cass-semantic-prod.md`。

## 7. Scaling 风险
- 全量重摄入成本随语料涨（当前 2862 conv ~42min 语义）；增量 pull 便宜（memoization：已嵌内容 0 GPU）。
- 整合 `index --semantic` 的 stall-abort 是上游已知 bug；坚持 backfill 循环路径。

## 待办（非阻塞）
- [ ] Phase C：jarvis 干净分支部署 `cass-index-daily`（M3 落地后协调）。
- [ ] M3 重接清单（M3 激活前）。
- [ ] 删冗余备份 `.preswap` + v1-trash（确认无需回滚后）。
