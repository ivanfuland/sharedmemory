# everos_adapter RUNBOOK

## M1a cap 的时序 gate

**判定时间**：2026-07-13（Task 6 实施时）
**判定命令**：
```bash
cd ~/projects/sharedmemory-everos-adapter
uv run python -c "
try:
    from cass_corpus.pruner import DeterministicPruner as P
except ImportError:
    print('6-B: cass_corpus.pruner 不可 import -> NoopClamper + 技术债'); raise SystemExit
print('6-A: _clamp 可用 -> PrunerClamper' if hasattr(P, '_clamp')
      else '6-B: 类存在但无 _clamp（master 现状）-> NoopClamper + 技术债')
"
```

**判定结果**：`6-A: _clamp 可用 -> PrunerClamper`

`cass_corpus.pruner.DeterministicPruner._clamp` 已在当前 HEAD（370e964 所在分支）的
`cass_corpus` 依赖中可用。`make_clamper()` 走的是 `hasattr(DeterministicPruner, "_clamp")`
探测——本次探测为真，`make_clamper()` 在本环境下实际返回 `PrunerClamper`，委托
`cass_corpus._clamp(content, cap, rescue_errors=True)` 做真实截断。

`NoopClamper` + `IS_TECHNICAL_DEBT` 分支仍保留在 `cap.py` 中，但当前**不是**生效路径——
它只作为防御性回退：若未来某个环境的 `cass_corpus` 版本回退到旧版 `DeterministicPruner`
（存在 `_truncate_observation` 但无 `_clamp`），`make_clamper()` 会显式降级为 `NoopClamper`
并在 stderr 打印警告，而不是让 `PrunerClamper.clamp()` 在调用时炸 `AttributeError`
（这条防御是 `test_make_clamper_falls_back_when_pruner_lacks_clamp` 专门覆盖的场景）。

无需偿还技术债——当前环境本就没有走 6-B 路径。

## Task 7：命令分布测量（$0 只读，为 RTK 决策备料）

**状态**：脚本 + 单测已实现并 GREEN（`scripts/measure_command_distribution.py` +
`everos_adapter/tests/test_measure_command_distribution.py`，10 passed，含合成
`:memory:`/`tmp_path` sqlite fixture 对 `collect()` 本身的配对 + 分类断言）。

**注意**：`--limit` 只限制第二趟 `tool_result` 查询；第一趟 `tool_call` 全量加载
是无界的（`collect()` 里 `calls` dict 一次性读完整表）。对着大候选库跑之前先给
DB 做体积上限或窄化范围，不要指望 `--limit` 能省下第一趟的内存/IO。

**Step 5（对候选库 `~/.local/share/coding-agent-search.new-6role/agent_search.db`
真跑一次只读测量）本次未执行**——按本次任务的编排指令明确列为 out of scope：
彼时有并发 session 正在使用真实 CASS 库（写入中），且该库属于「候选/生产库」，
不在本 M1a TDD 任务的写权限范围内。脚本已具备 `mode=ro` 只读打开能力，命令行
入口就绪：

```bash
uv run python scripts/measure_command_distribution.py \
  --db ~/.local/share/coding-agent-search.new-6role/agent_search.db
```

留待后续单独一步执行，并把输出补进本节，作为 spec §5.2 RTK 决策的依据。
