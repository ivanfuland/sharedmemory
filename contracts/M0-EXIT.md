# M0 出口确认（契约确立完成）

> 21 契约测试全绿。读端路线 = canonical。GBrain/CASS 真实 API 已坐实，多处原生 de-risk。

## 出口判据（spec §5 P0）逐条
- [x] ① 读端契约成立——canonical：sqlite3 兼容 + 逐字段非空 + 游标 INTEGER PK 单调唯一无 NULL；DECISION 落档（不走 fallback）
- [x] ② 六类契约全绿（真实 text-CLI API）：class1-4/6 本地 + class5 HTTP 层鉴权 401；scope 细粒度→M2、stale 同步正例→M3（均显式记录，非假绿）
- [x] ③ 读腿（canonical JOIN 可执行，必需字段非空）+ 写腿（timeline-add reconcile 幂等）；完整桥串联→M3
- [x] ④ 中文 wikilink 原生 directional backlink，无需拼音降级

## 跑测命令（复现）
```bash
cd ~/projects/sharedmemory
export PATH="$HOME/.bun/bin:$PATH"
export GBRAIN_HOME="$PWD/sandbox/gbrain"   # 缺则 scripts/gbrain-sandbox-up.sh
CASS_CANON_DB=~/.local/share/coding-agent-search/agent_search.db uv run pytest -v
```

## M0 实测对 spec/M3 的关键修正（写 M1+ plan 必读）
1. **CASS 装机 = baseline 源码构建**（预编译 glibc 2.38 不兼容；完整构建 ONNX 链接失败）。语义层禁用=P5 范围
2. **读端 = canonical 规范化 schema**，须 JOIN（messages+conversations+agents+workspaces），游标 messages.id 全局单调
3. **GBrain text-CLI**：put(stdin md)/get(md)/timeline-add(位置参,原生去重)/search(只索引 body 不索引 timeline)；idempotency 靠 timeline 文本扫描
4. **原生 de-risk**（spec 担心点被消除）：中文 slug+wikilink OK、CJK 词法搜索 OK（CASS+GBrain 都行，与 004 §五"半残"预测相反）、timeline 去重、(stale) 标记
5. **serve --http = OAuth 2.1**（无 gbrain auth CLI；scoped client 经 /admin UI 或 DCR）→ 三端接入 scope 隔离是 M2 实活
6. **嵌入未验**：M0 沙盒 --no-embedding；BGE-M3/query 混合检索是 M1

## 下一步：M1（Ollama bge-m3+qwen3.6:27b + pg-memory 容器 + 负载基准）
