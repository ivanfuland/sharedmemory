# M3 出口确认（蒸馏桥核心）

> **第一行 framing（binding）**：M3 = 蒸馏桥核心管道（state/read/filters/distill/writer/reconcile/stale/hygiene/run/quality/crash/backup），夜批调度器（Inngest cron）已建但**尚未部署到生产 jarvis-workflow-ts**，live 端到端（真 CASS → 真 gbrain）未执行。本出口确认不等于「夜批已上线」。

---

## 出口判据逐条（spec §5 P3 + §2.6 + §11）

- [x] **全 `tests/test_m3_*.py` 绿**：57 passed（state/read/filters/distill/writer/reconcile/stale/hygiene/run/quality/crash/backup，2026-06-24 实跑；原 53 + Task 13 新增 4 backup tests）
- [x] **质量门：gpt-5.4-mini P≥0.9 / R≥0.8，model_lock=locked**：22 样本，P=0.923 / R=0.857 / F1=0.889；`config/m3-bridge.json` `model_lock.status=locked`，`locked_at=2026-06-24T06:09:52Z`（Task 11 实跑，test_m3_quality 绿）
- [x] **crash-injection 七断点零丢失/零重复/total_backlog 可见**：8 测绿（7 个 crash point 全覆盖，每点重启后幂等恢复，backlog 计数一致）；（Task 12，test_m3_crash_injection 8 passed）

  > 注：brief 写「七断点」，实现覆盖 8 个注入场景，全过。

- [x] **备份覆盖 canonical+raw mirror+brain+bridge state；restore smoke 通 + last-restore-ok 留档**：`infra/backup/backup-brain.sh` 已加 bridge-state.db 备份（`sqlite3 .backup` 原子拷）+ restore smoke 验证 + 时间戳文件；test_m3_backup 4 passed（Task 13）
- [ ] **Inngest `distill-bridge-daily` 注册可见 + 手动 invoke 端到端跑通（真 CASS 增量 → 真 gbrain 写 → 联邦读回）**：**DEFERRED**——Inngest cron 函数代码骨架（`infra/distill/run-bridge.sh` + `distill/run.py` CLI 入口）已完整；但 `jarvis-workflow-ts` 侧 route 文件**未部署**（`src/inngest/functions/distill-bridge.ts` 不存在），Inngest dev server 中看不到此函数；live end-to-end 从未跑过真实 CASS/gbrain（测试全用 sandbox + 合成数据）。门控：需 CASS schema 指纹漂移确认 + 真 API key 入 config.env + runner.ts async-spawn 接线 + Ivan 人工审批激活。
- [ ] **完整夜批 ≤2h（spec §12.4 时间门）**：**DEFERRED**——从未对真实数据量跑过计时（测试数据 <20 条）；本约束的验证依赖 live 激活后首次真跑。
- [ ] **/ingest 全程未用（全走 OAuth /mcp）；蒸馏走 API key（审计日志有记录，零订阅 OAuth）**：架构层已保证（writer.py 走 `/mcp` + `config.env` 指定 `DISTILL_API_KEY` = LiteLLM key，非订阅 OAuth）；**但 live 审计日志（`infra/distill/audit.log`）无真实条目**（从未对生产运行），所以「审计日志有记录」不能打钩。架构正确，live 验证待激活后补。
- [ ] **stale 正例：桥写 contradicts-truth 条目 → 页 search 行现 `(stale)`**：`distill/stale.py` 实现已完整（Task 7，test_m3_stale 绿）；但**真 gbrain 上的 stale 同步正例（M0 未能验的那条）仍未跑过**——sandbox gbrain 下测试只验 writer 调用路径，未验 gbrain 原生 stale 标记传播。

---

## 对 M4 的修正（实测回填）

- **gbrain 工具名最终确认**（M2 probe 修正，M3 沿用）：`put_page{slug, content}`、`add_timeline_entry{slug, date, summary}`、`get_page{slug}`、`search{query}`、`get_timeline{slug}`；传输=SSE（非纯 JSON）。
- **`get_page` not-found 行为**：Task 5 live probe **已在真 gbrain 上确认**——缺页抛 `McpError("Page not found")`；`writer._page_exists` 判据已对齐（认 not-found marker + 空文本两种）。
- **崩溃恢复约束完全落地**：①游标+raw_work_item 同事务；②distill phase 单事务内 校验→journal INSERT OR IGNORE→raw 标 distilled；③replay UPDATE WHERE 断言 affected==1；④total_backlog 口径 = raw_backlog + journal_backlog（read 后崩可见）——全部经 crash-injection 8 点验证。
- **CASS 指纹实测**：`contracts/cass-canonical.fingerprint` 已写；但真实 CASS DB（`~/.local/share/coding-agent-search/agent_search.db`）上的指纹比对从未在运行时触发过（cass_reader.verify_fingerprint 只在 test sandbox 路径下跑）。M4 激活前须先对真 CASS DB 跑一次 verify。

---

## 复现命令

```bash
cd ~/projects/sharedmemory && export PATH="$HOME/.bun/bin:$PATH"
set -a
source infra/gbrain/config.env
source infra/pg-memory/.env
source infra/distill/config.env
set +a
export CASS_CANON_DB=~/.local/share/coding-agent-search/agent_search.db
export GBRAIN_HOME="$PWD/sandbox/gbrain-pg"

# 全 M3 测试（57 tests）
uv run pytest tests/test_m3_*.py -v

# 备份脚本 dry 验证（bridge state db 不存在时 INFO 跳过，不报错）
bash infra/backup/backup-brain.sh

# restore smoke 独立验
sqlite3 /tmp/bridge-test.db "
  CREATE TABLE IF NOT EXISTS cursor(id INTEGER PRIMARY KEY, source_id TEXT, stream_position INTEGER);
  CREATE TABLE IF NOT EXISTS raw_work_item(id INTEGER PRIMARY KEY, source_id TEXT);
  INSERT INTO cursor VALUES(1,'test',99);
"
bash infra/backup/restore-bridge-smoke.sh /tmp/bridge-test.db /tmp/bridge-restored.db
```

---

## gbrain / CASS 版本锁

- **gbrain**: `0.42.37`（`scripts/gbrain-version.txt`）
- **CASS 指纹**: `contracts/cass-canonical.fingerprint`（SHA256 of schema DDL）
- **蒸馏模型锁**: `gpt-5.4-mini`（`config/m3-bridge.json` `model_lock.status=locked`）

---

## M4 激活前置清单（DEFERRED 项的解锁门控）

1. **CASS 指纹漂移确认**：对真实 CASS DB 跑 `cass_reader.verify_fingerprint`，无 schema 变化再激活
2. **真 API key 入 config.env**：当前 `DISTILL_API_KEY` 是 LiteLLM 测试 key，生产须确认额度
3. **jarvis-workflow-ts runner.ts async-spawn 接线**：把 `infra/distill/run-bridge.sh` 挂进 Inngest function route.ts，deploy 到 dev server，手动 invoke 验一次
4. ~~**`get_page` not-found probe**~~：✅ Task 5 已 probe 确认（缺页抛 McpError "Page not found"），_page_exists 已对齐
5. **stale 正例 live 验**：手动写一条 contradicts-truth timeline，confirm `gbrain search` 返回 `(stale)`
6. **≤2h 时间门**：首次真跑记录 wall-clock，确认不超限
7. **Ivan 人工审批**：所有上述 6 项 PASS 后，人工确认激活夜批 cron

## ★ Scoped live e2e（2026-06-24 实测，部分解除 deferred）
- 真 CASS→真 gpt-5.4-mini→真 gbrain 一条 span 跑通：4 页落库带 provenance+[dk:]+出机审计；游标追平 re-run 零写（idempotency no-op）。
- 仍 deferred：全量夜批规模 + ≤2h 时间门 + 全 source + jarvis async-spawn 部署 + 激活审批。
- e2e 修复：TG opt-in 门(BRIDGE_TG_NOTIFY)、slug 小写化、CASS 指纹更新（additive drift 良性）。
