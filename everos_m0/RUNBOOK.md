# EverOS M0 RUNBOOK

M0 = 纯 plumbing 冒烟。目标：喂一条合成 6-role 会话 → EverOS 异步提炼 → 从 markdown
输出扫到该 session 的 `agent_case`，证明「CASS→适配器→EverOS→markdown-scan 终态」通。

- Spec: `cc-workspace/docs/projects/shared-memory/specs/2026-07-08-everos-coding-memory-pipeline-design.md`
- Plan: `cc-workspace/docs/projects/shared-memory/plans/2026-07-08-everos-m0-plumbing-plan.md`
- EverOS 当**黑盒**（公开面 = HTTP `/add`·`/flush`·`/search` + markdown 输出 + `everos cascade sync`），不 fork。
- 全隔离：`--root /tmp/cc-everos-m0/.everos`。**不碰真 `~/.everos`、不碰 gbrain/CASS 生产。**

---

## 1. Deploy（Task 1）

### 1.1 装依赖

```bash
cd ~/projects/EverOS && uv sync          # 拉 everalgo-agent-memory==0.3.1 等提炼包
cd <sharedmemory worktree> && uv add httpx   # httpx 原为传递依赖，显式声明
```

EverOS 仓保持零源码改动（黑盒约束）。`uv sync` 后 `uv run everos --help` 应列出
`init` / `server` / `cascade` / `demo`。

### 1.2 生成隔离 root

```bash
mkdir -p /tmp/cc-everos-m0/.everos
cd ~/projects/EverOS
uv run everos init --root /tmp/cc-everos-m0/.everos --force
```

生成**两个**文件（spec 只提到前者）：

| 文件 | 作用 |
|---|---|
| `<root>/everos.toml` | provider / api / sqlite / lancedb / memorize 配置 |
| `<root>/ome.toml` | **OME 策略配置的单一入口**（enabled / cron / gate），热重载 ~2s |

`[memorize] mode = "agent"` 是 shipped default（spec §4.1 判断正确）。

### 1.3 配置优先级（M0 discovery ① — 已关闭）

`everos init --print` 的模板注释写死了 lookup order：

```
1. shipped defaults (lowest)
2. <root>/everos.toml
3. 环境变量 EVEROS_<SECTION>__<KEY>
4. programmatic init args (highest)
```

**env (3) > toml (2)** → M0 全走 `EVEROS_*` env 注入，**api_key 绝不落盘**。
plan 里那条 fallback（「若 file > env 则改为直接编辑 toml」）**不需要走**。

root 解析：`resolve_root(): EVEROS_ROOT env > ~/.everos`。

### 1.4 起 server

secrets 从 `/tmp/cc-everos-m0/env.sh`（`0600`，不进 git，M0 收尾随隔离目录清除）读：

```bash
source /tmp/cc-everos-m0/env.sh
cd ~/projects/EverOS
EVEROS_MEMORIZE__MODE=agent \
EVEROS_LLM__MODEL=deepseek-v4-flash \
EVEROS_LLM__API_KEY="$EVEROS_M0_KEY" \
EVEROS_LLM__BASE_URL="$LITELLM_LLM_BASE" \
EVEROS_EMBEDDING__MODEL=BAAI/bge-m3 \
EVEROS_EMBEDDING__API_KEY=EMPTY \
EVEROS_EMBEDDING__BASE_URL="$INFINITY_BASE" \
EVEROS_RERANK__PROVIDER=vllm \
EVEROS_RERANK__MODEL=BAAI/bge-reranker-v2-m3 \
EVEROS_RERANK__API_KEY= \
EVEROS_RERANK__BASE_URL="$INFINITY_BASE" \
EVEROS_API__HOST=127.0.0.1 EVEROS_API__PORT=8000 \
setsid uv run everos server start --root /tmp/cc-everos-m0/.everos \
  > /tmp/cc-everos-m0/server.log 2>&1 < /dev/null &
```

**⚠️ 偏离 plan：pidfile 必须存 PGID，不能存 `$!`。**
`$!` 抓到的是 `setsid` 自己，它 fork 出真进程后立即退出 → pidfile 里是个死 pid。
真实进程树是 `uv run everos`（PGID leader，PPID=1）+ `python .../everos`（同组）。

```bash
REAL_PID=$(pgrep -f 'everos server start --root /tmp/cc-everos-m0' | head -1)
ps -o pgid= -p "$REAL_PID" | tr -d ' ' > /tmp/cc-everos-m0/server.pgid
```

停服（Task 6）：`kill -TERM -- "-$(cat /tmp/cc-everos-m0/server.pgid)"`（杀整组，
codex R0#8 预见的 uv-wrapper-fork 坑）。杀前确认该 PGID ≠ 当前 shell 的 PGID。

### 1.5 冒烟验收（Task 1 Step 4）

| 检查 | 结果 |
|---|---|
| `curl /health` | `200 {"status":"ok"}`（`/docs` 默认 404，别用它探活） |
| 数据落隔离目录 | `<root>/.index/{lancedb,sqlite}` + `<root>/.tmp` |
| 真 `~/.everos` | **不存在**（M0 起跑前就不存在，收尾须仍不存在） |
| 启动日志 error 数 | 0 |
| **env 覆盖生效的正面证据** | 日志 `llm_client_built model=deepseek-v4-flash` |
| shipped default 未被用 | 日志中 `deepinfra` / `gpt-4.1-mini` / `gemini` 各 0 次 |
| 绑定 | `ss -tlnp` 显示 `127.0.0.1:8000`（非 `0.0.0.0`） |

> 验证 error 数时**不要**用 `grep ... | head -N || echo ok` —— `head` 的退出码会盖掉
> `grep` 的，永远走不进 `||` 分支（PIPESTATUS 坑）。直接 `grep -c` 取数。

---

## 2. 配置要点（踩过的）

- **`[rerank] provider` 必须显式 `"vllm"`**。不设则从 `base_url` host 推断，未知 host
  （`127.0.0.1`）回落 `"deepinfra"`，请求形状就错了。`vllm` → `POST {base_url}/rerank`。
- **`[rerank] api_key = ""`（空串）**，非 `"EMPTY"`：非空会发出伪 `Bearer` 头。
  embedding 反之，SDK 要非空占位 → `"EMPTY"`。
- **`[embedding]` 无 shipped default**，`model`/`api_key`/`base_url` 三者必须显式设。
- **`[multimodal]` 默认 `google/gemini-3-flash-preview`** —— 我们的 LiteLLM key 只放行
  两个 deepseek 模型。M0 喂纯文本不触发（日志 `gemini` 命中 0 次）。若将来喂图/PDF，
  这里会 401/404，需另配或禁用。
- **`cascade` 的 `--root` 在 group 上**，不在 `sync` 子命令上：
  `everos cascade --root <root> sync`（或 `EVEROS_ROOT=<root> everos cascade sync`）。
  另有 `cascade status` 可看队列/LSN 摘要，比 grep 日志好用。

## 3. 成本杠杆（对 spec §10 的修正）

spec §10 写：`mode=agent` 会顺带跑 user 侧 pipeline，写侧 LLM ≈ 翻倍；「若成本不可接受，
再评估 **fork EverOS** 关 user pipeline（非目标，尽量不做）」。

**实测：不用 fork。** `<root>/ome.toml` 是 OME 所有策略的单一入口，支持
`[strategies.<name>] enabled = false` 逐个关闭，**热重载 ~2 秒、无需重启**，且未知 key 会
抛 `StartupValidationError`（不会静默错配）。user 侧可关的策略：

- `extract_atomic_facts`（per memcell）
- `extract_foresight`（per memcell，注释自称 "Heavy LLM call — common to disable"）
- `trigger_profile_clustering`（counter-gated，默认 threshold=5）
- `extract_user_profile`
- `reflect_episodes`（默认已关）

**M0 不关**：Task 6 要测的正是真实 per-memcell LLM 调用基线，关了就测不到。
这是 **M1 的成本优化项**——spec 里那条「不可接受就 fork」的退路，降级成改一行配置。

## 4. 对 plan 的显式偏离（累积）

1. **pidfile 存 PGID 而非 `$!`**（§1.4）——`$!` 是 setsid 自己，已死。
2. **preflight 需要两个 base**（Task 2）：LiteLLM 的 `/key/info` 挂在 **root**
   （`https://<host>/key/info`），`/v1/key/info` 实测 **404**。而 EverOS 的 `[llm] base_url`
   必须带 `/v1`。故 `check_litellm_budget(admin_base, ...)` 与 `[llm] base_url` 是两个值，
   plan 里 `f"{base}/key/info"` 单参数签名会拼出 404 路径。

## 5. agent_case 的两道准入门（M0 最大的意外发现）

**不是每条 coding 会话都会产出 `agent_case`。** everalgo 有两道门，都在 EverOS 之外
（`everalgo/agent_memory/case.py`），不可经 `everos.toml` / `ome.toml` 配置：

### 门 1 · 结构（`_should_skip` + `min_tool_call_rounds`）
- `ToolCallRequest` 轮数 **≥ 3**（`min_tool_call_rounds=3` 是 `AgentCaseExtractor.__init__`
  的默认参数，EverOS 构造时不传 → 写死）
- **末条消息必须是 assistant 的 ChatMessage**（非 ToolCallRequest/Result），否则判
  "Incomplete agent trajectory"
- 需有 user 锚（`_strip_before_first_user` 会丢掉首条 user 之前的一切）

### 门 2 · LLM 语义（`prompts/case_filter.py`）
判「这条轨迹值不值得学」。**顺风顺水的线性流程一律拒**。原文判据：

> Set False when the agent executed a known, linear procedure with no surprises.
> **Complexity lives in the transferable lesson or the hard-won path, not in the
> number of tool calls.**

要过门必须有 *hard-won discovery*：走错路后回退，或撞上非显然的根因。
实测被拒的 reason：`"Straightforward march: run test, read source, edit, verify
— no detours or surprises."`

### 对 spec / 下游的影响
- **plan 原 fixture（1 轮 tool-call）永远产不出 agent_case**，端到端会神秘 TIMEOUT。
  现 fixture = 7 轮 + dead end + 意外根因，两道门已编码成 `test_role_map.py` 的断言。
- **spec §6.2 的终态机需要区分三态**，不能只有 `extracted` / `pending`：
  `agent_case_skipped_by_algo` 是**确定性的合法「不产出」**（终态），不是「还没好」。
  当 pending 处理会导致每次 retry 重扫、永远 pending。台账应写 **empty tombstone**。
- **M1 测召回时，「喂 N 条会话只出 M 张卡片」是正常的，M ≪ N。** 这是 EverOS 的设计
  （只学值得学的），不是 bug。

## 6. 观测记录（M0 实测）

### 6.1 U-5a owner 落点 ✅ 与 spec §5.1 一致
```
agents/ivan-coding/.cases/agent_case-2026-07-09.md    <- assistant/tool_call 的 sender
users/ivan/episodes/episode-2026-07-09.md             <- user 消息的 sender
users/ivan/.atomic_facts/atomic_fact-2026-07-09.md
```

### 6.2 异步时序（codex R0#3 说对了）
- `feed 开始 → /flush 返回`：8s（`/add` 200 + `/flush` 200，status=`extracted`）
- `/flush 返回 → agent_case 落盘`：**5s**（异步）
- **`/flush` 返回 `extracted` ≠ 提取完成**。终态只能由「扫到 markdown 产物」确认。
- **不需要手动 `cascade sync`**（M0 discovery ③ 关闭）：OME 自动 drain，
  `cascade status` 显示 `pending: 0, done: 3, failed: 0`。

### 6.3 per-memcell 成本（1 条 fixture 会话 = 701 tokens = 1 memcell）

| 策略 | 侧 | run |
|---|---|---|
| `extract_agent_case` | agent | 1 |
| `trigger_skill_clustering` | agent | 1 |
| `extract_atomic_facts` | user | 1 |
| `trigger_profile_clustering` | user | 1 |
| `extract_user_profile` | user | 1 |
| **合计** | | **5 run / memcell** |

- spec §10 估的「每 memcell ≥3–5 次 LLM」**实测准确**（5）。
- 但「mode=agent 使写侧 LLM ≈ 翻倍」应修正为：**user 侧占 5 个 run 里的 3 个（60%）**。
  关掉 `ome.toml` 的三个 user 策略可省约 60% 写侧调用。
- **真实花费**：`spend` delta = **$0.000456 / memcell**（deepseek-v4-flash）。
  外推 1000 个同规模 memcell ≈ **$0.46**；`max_budget=100` 够跑约 21.9 万个。
  ⚠️ fixture 仅 701 tokens，真实 coding 会话大得多且可能切多个 memcell，
  按 token 线性放大后再估算 —— 但绝对量级表明 **spec §10 对成本的焦虑被证伪**。
- **无 `agent_skill` 产出**：skill 是跨 session 涌现（cluster 驱动），单会话不够聚簇。
  符合 spec §6.2「skill 不做 per-session 终态」。

### 6.4 U-4a 关闭：提炼器**确实**消费 tool 结构
`agent_case` 的 Approach 逐条复述了 tool 交互的具体内容——`pytest tests/test_foo.py -x`
命令、`E assert 1 == 2` 报错原文、`git checkout --` 回退、commit hash `a1b2c3d`、
`src/bar.py:20` 的 `return foo() + 2`。这些只可能来自 `tool_call.args` 与
`tool_result.content`。spec §4.3 那条「`CanonicalMessage` 注释说 only type=text is
parsed downstream」的疑虑，在 agent 路径不成立。→ §5.2 无需退化为 tool→text 渲染。

### 6.5 输出语言
- `agent_case`（我们要的）：**中文**（跟随输入语言）
- `users/*/episodes`（v1 不消费）：**英文**（everalgo 的 episode prompt 强制英文）

## 7. 安全事故记录（M0 自捅，已修）

第一版启动脚本用 `source /tmp/cc-everos-m0/env.sh && setsid uv run everos server start`，
`export` 的变量被子进程继承 → **`LITELLM_ADMIN_KEY`（admin 级、无预算上限）泄漏进
EverOS 的 `/proc/<pid>/environ`**。EverOS 是黑盒第三方进程，能读自己的 environ。

- **同 MEMORY「spawn 子进程别 `{...process.env}` 全泄漏」的 bash 版。**
- 修复：`start-server.sh` 改用 `env -i` + 白名单（只给 `PATH/HOME/LANG` + `EVEROS_*`，
  其中只含 everos-m0 那把有限额推理 key）。
- 验证：`tr '\0' '\n' < /proc/<pid>/environ | grep KEY` 只剩
  `EVEROS_LLM__API_KEY` / `EVEROS_EMBEDDING__API_KEY=EMPTY` / `EVEROS_RERANK__API_KEY=`（空串）。
- **建议 M0 收尾轮换 admin key**（它曾在进程 env 与终端历史中出现）。
