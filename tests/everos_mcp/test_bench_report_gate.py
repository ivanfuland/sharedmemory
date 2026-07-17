# tests/everos_mcp/test_bench_report_gate.py
"""scripts/everos_mcp_bench.py::_build_report 的纯函数测试(P1e/P1i)。

固定纪律(见任务简报 P1e/P1i):此前每轮重试机制(修一个已经修好的 harness bug
时加的)会把真实的传输层错误"洗掉"——重试成功后整轮样本照常计入 effective,
gate 只看 p95,不受影响;非 hit 状态(理论上 bench 合成拓扑下不该出现)也
不影响 gate。这两者都必须收紧为:
- **任何一轮**(含被丢弃的热身轮)出现非 "hit" 状态 -> FAIL(不再放行
  abstain_empty)。热身轮只对 p95 计时门槛不计分(JIT/tokenizer 懒加载/首轮
  冷 blob 写入不该污染延迟统计),但这不代表热身轮里出现的非 hit 状态可以被
  容忍——P1i 修复前的实现只检查 effective 集合,热身轮里的非 hit 状态会被
  完全忽略,这是本文件曾经的一个假阴性来源(见
  `test_non_hit_status_in_warmup_round_fails_gate`,此前反向断言"不影响
  gate",已反转为"必须 FAIL")。
- 任何一轮需要重试(attempts > 1)-> FAIL,并在报告里点名是哪几轮。
- 通过的 run 必须是 0 retries、100% hit(含热身轮)——重试机制本身保留
  (诊断用),但"跑通过 = 零重试 + 全程 100% hit" 是新增门槛,不是"重试成功
  也算过"或"热身轮随便出错也没事"。

不依赖真实 server/网络——只测 `_build_report` 这个纯函数,构造合成 samples/
round_retries 输入。
"""
from __future__ import annotations

from scripts.everos_mcp_bench import _build_report


def _sample(round_idx, query_index, ms, status="hit", cold=False):
    return {
        "round": round_idx, "query_index": query_index, "ms": ms,
        "cold_first_seen": cold, "status": status,
    }


def _all_hit_samples(reps=5, per_round=3, ms=10.0):
    return [
        _sample(r, q, ms, status="hit", cold=(r == 0))
        for r in range(reps)
        for q in range(per_round)
    ]


def test_all_hit_zero_retries_passes():
    samples = _all_hit_samples(reps=5, per_round=3)
    round_retries = [{"round": r, "attempts": 1} for r in range(5)]
    report = _build_report(samples, round_retries, discard_rounds=1, reps=5)
    assert report["gate"]["passed"] is True
    assert report["gate"]["all_hit_ok"] is True
    assert report["gate"]["zero_retries_ok"] is True
    assert report["total_retries"] == 0
    assert report["retried_rounds"] == []
    assert report["non_hit_statuses"] == []


def test_non_hit_status_in_effective_sample_fails_gate():
    samples = _all_hit_samples(reps=5, per_round=3)
    # 把第 3 轮(effective,discard_rounds=1 时 round>=1 都是 effective)的一条
    # 样本改成 abstain_empty——曾经这类状态被当成"可接受",现在必须让 gate FAIL。
    samples[10] = dict(samples[10], status="abstain_empty")
    round_retries = [{"round": r, "attempts": 1} for r in range(5)]
    report = _build_report(samples, round_retries, discard_rounds=1, reps=5)
    assert report["gate"]["passed"] is False
    assert report["gate"]["all_hit_ok"] is False
    assert report["non_hit_statuses"] == ["abstain_empty"]


def test_non_hit_status_in_warmup_round_fails_gate():
    """P1i(反转此前假设):热身轮(round < discard_rounds)只对 p95 计时
    门槛不计分——JIT/tokenizer 懒加载/首轮冷 blob 写入不该污染延迟统计。但
    这不代表热身轮里出现的非 hit 状态可以被容忍:这套 bench 合成拓扑本就
    始终有候选,任何一轮(不管算不算 effective)出现非 hit 状态都是真故障
    或 stub 拓扑坏了。此前实现只检查 effective 集合,热身轮的非 hit 状态会
    被完全忽略、gate 照样 PASS——这是一个假阴性,必须反转为 FAIL。"""
    samples = _all_hit_samples(reps=5, per_round=3)
    samples[0] = dict(samples[0], status="abstain_empty")  # round=0,被丢弃的热身轮
    round_retries = [{"round": r, "attempts": 1} for r in range(5)]
    report = _build_report(samples, round_retries, discard_rounds=1, reps=5)
    assert report["gate"]["passed"] is False
    assert report["gate"]["all_hit_ok"] is False
    assert report["non_hit_statuses"] == ["abstain_empty"]


def test_round_needing_retry_fails_gate_even_if_all_hit():
    samples = _all_hit_samples(reps=5, per_round=3)
    round_retries = [{"round": r, "attempts": 1} for r in range(5)]
    round_retries[2] = {"round": 2, "attempts": 3}  # 第 3 轮重试了 2 次才成功
    report = _build_report(samples, round_retries, discard_rounds=1, reps=5)
    assert report["gate"]["passed"] is False
    assert report["gate"]["zero_retries_ok"] is False
    assert report["total_retries"] == 2
    assert report["retried_rounds"] == [{"round": 2, "attempts": 3}]


def test_p95_over_threshold_still_fails_gate():
    samples = _all_hit_samples(reps=5, per_round=3, ms=5000.0)
    round_retries = [{"round": r, "attempts": 1} for r in range(5)]
    report = _build_report(samples, round_retries, discard_rounds=1, reps=5)
    assert report["gate"]["passed"] is False
    assert report["gate"]["p95_ok"] is False


def test_report_still_has_effective_warm_path_stats():
    """新字段是加法,不破坏既有 p50/p95/p99/min/max/mean 报告结构(下游可能
    读这些字段——不做无谓的破坏性重命名)。"""
    samples = _all_hit_samples(reps=5, per_round=3, ms=42.0)
    round_retries = [{"round": r, "attempts": 1} for r in range(5)]
    report = _build_report(samples, round_retries, discard_rounds=1, reps=5)
    stats = report["effective_warm_path"]
    assert stats["p50_ms"] == 42.0
    assert stats["p95_ms"] == 42.0
    assert stats["sample_count"] == 4 * 3  # reps=5, discard 1 round -> 4 effective rounds
