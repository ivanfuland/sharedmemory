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
