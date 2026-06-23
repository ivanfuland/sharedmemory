# M1 出口确认（基建完成，2026-06-23）

> 嵌入方案 v12（spec §12.8）：openrouter:text-embedding-3-small@1536 经 LiteLLM → OpenAI + Postgres 后端。
> 全部 M1 测试 10/10 绿（真打用户 LiteLLM）。codex v12 R2 PASS（架构）。

## 出口判据（spec §12.4 + §12.8 + §2.5.1）逐条
- [x] pg-memory Postgres 起（stock pgvector，无 zhparser）：vector/pg_trgm/pgcrypto 可建 + vector(1536) 列可建（Task1，2 测试绿）
- [x] **GBrain Postgres + openrouter:text-embedding-3-small@1536 经 LiteLLM**：config `embedding_dimensions==1536` + `embedding_model` 以 `openrouter:` 开头 + **embed 往返硬门真通**（put/embed fail-loud + distractor 排名 query 把目标页排第一）——实测经 LiteLLM 真嵌入（Task2）
- [x] **GBrain 六类契约 + serve --http 在 Postgres+嵌入 后端 9 测全绿**（§2.5.1 P1 复跑；M0 是 PGLite --no-embedding）
- [x] 云蒸馏 smoke：gpt-5.4-mini 经 LiteLLM strict json_schema + 输出校验 + 审计日志（Task3，守门拦缺 env）
- [x] 负载基准产出 `config/m1-benchmarks.json` 含 derived_config（Task4，4 测试绿）

## 服务起停 / 回滚
```bash
# pg-memory
cd infra/pg-memory && docker compose up -d        # 起
cd infra/pg-memory && docker compose down -v       # 回滚（删容器+卷；postgres:16 业务库不受影响）
# GBrain（Postgres 后端 + LiteLLM 嵌入）
set -a; source infra/gbrain/config.env; source infra/pg-memory/.env; set +a
infra/gbrain/gbrain-pg-up.sh                        # init（幂等：config.json 在则跳过）
```

## 实测结论（关键）
- **openrouter-shim → LiteLLM 路由坐实**：直探 LiteLLM `/v1/embeddings` text-embedding-3-small 返回 **dim=1536**；gbrain 经 openrouter recipe（openai-compatible，认 OPENROUTER_BASE_URL）嵌入往返真通。LiteLLM 模型清单含 text-embedding-3-small/large + gpt-5.4-mini ✓。
- **native `openai` recipe 不可用于嵌入路由**：其嵌入 `createOpenAI({apiKey})` 写死官网、不读 base_url（gateway.ts:1115-1126）→ 必须用 openrouter recipe（codex v12 R1 P0）。
- **GBrain Postgres 只需 vector+pg_trgm+pgcrypto，无 zhparser**（schema.sql:3-6）→ stock pgvector 镜像即可，无自建 Dockerfile。

## 基准数字（`config/m1-benchmarks.json`，corpus span 8.0d / 300 样本）
- 嵌入吞吐（LiteLLM/text-embedding-3-small）：best **batch 64 → 13.3 embeds/s**（p95 5.03s/batch）；batch 32 → 8.9；batch 16 → 6.6
- 蒸馏延迟（gpt-5.4-mini）：conc1 p95 **2.17s**，conc2 p95 8.62s（抖动），conc4 p95 3.79s；error_rate 全 0
- **derived_config**（M3 消费）：`embed_batch_size=64`、`distill_concurrency=1`（保守：conc2 p95 8.62s 超 2×conc1 阈值）、`distill_timeout_s=90`
> 注：云端延迟有抖动，数字随跑波动（如 conc2 p95 在 3.6~8.6s 间）；以 `config/m1-benchmarks.json` 当前值为准。
- 注：「完整夜批 ≤2h」时间门 M3 真桥验（M1 仅延迟 probe + 参数）

## 对 M3 的修正（执行时实测发现）
1. **长消息必须分块**：实测样本 max **317107 字符**（工具/代码 dump），撞 text-embedding-3-small 8191-token 上限 → 400。基准截断到 6000 字符量吞吐；M3 蒸馏桥须对长消息分块后嵌入。
2. **脚本 ROOT 上溯**：`infra/gbrain/gbrain-pg-up.sh` 在两级深目录，`ROOT` 须 `$(dirname "$0")/../..`（plan 原写 `/..` 错，已在仓内修正）。
3. **distill_concurrency 实测保守取 conc1**：小样本下 conc2 p95 抖动超阈；M3 真批量可重测放宽。
4. **Docker registry mirror 坑**：本机 `docker.1ms.run` mirror 对 pgvector:pg17 反复 `unknown blob`；去掉 mirror（daemon.json `{}` + restart docker）走直连即通。

## 复现命令
```bash
cd ~/projects/sharedmemory && export PATH="$HOME/.bun/bin:$PATH"
cd infra/pg-memory && docker compose up -d && cd -
set -a; source infra/gbrain/config.env; source infra/distill/config.env; set +a
export CASS_CANON_DB=~/.local/share/coding-agent-search/agent_search.db
infra/gbrain/gbrain-pg-up.sh
GBRAIN_HOME="$PWD/sandbox/gbrain-pg" uv run pytest tests/test_m1_*.py tests/test_gbrain_*.py -q
uv run python benchmarks/m1_load.py
```

## 下一步：M2（GBrain serve --http 单例 + 三端 scoped client + SessionStart hooks）
serve --http 多客户端并发/规模/durability 在 M2 实测（spec §12.7/§12.8 已记为已知未知）。
