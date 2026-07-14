import math
from everos_eval.stats import QueryOutcome, wilson_lower, compute_metrics, band_verdict

def _q(qid, rel, useful, top5, t5rel, t5use):
    return QueryOutcome(qid, frozenset(rel), frozenset(useful), tuple(top5),
                        frozenset(t5rel), frozenset(t5use))

def test_wilson_lower_known_value():
    # p̂=0.5, n=20, z=1.6449:center=0.56764, margin=0.19596, denom=1.13529 → ≈0.3274(codex 复核)
    assert math.isclose(wilson_lower(10, 20), 0.3274, abs_tol=0.001)
    assert wilson_lower(0, 0) == 0.0

def test_band_verdict_bands():
    assert band_verdict(0.55, 20, 0.30) == "strong_pass"
    assert band_verdict(0.45, 20, 0.30) == "borderline_pass"
    assert band_verdict(0.45, 20, 0.20) == "borderline_pass_exploratory"  # 过门但下界<0.25
    assert band_verdict(0.35, 20, 0.20) == "weak_signal"
    assert band_verdict(0.20, 20, 0.10) == "clear_fail"
    assert band_verdict(0.50, 9, 0.30) == "invalid_n"  # n<10 不判门

def test_compute_metrics_end_to_end():
    outs = [
        # covered 且 top5 有有用卡(hit 且 useful,有用卡在第 1 位)
        _q("q1", {"a", "b"}, {"a"}, ["a", "x", "y", "z", "w"], {"a"}, {"a"}),
        # covered,top5 捞到相关卡 b(第 3 位)但无有用卡;库里也没有用卡
        _q("q2", {"b"}, set(), ["x", "y", "b", "z", "w"], {"b"}, set()),
        # covered 但 top5 全 miss(库里有有用卡 c → 检索丢)
        _q("q3", {"c"}, {"c"}, ["x", "y", "z", "w", "v"], set(), set()),
        # uncovered(L1-no):top5 有 2 条被判"相关"→ 误召回画像素材
        _q("q4", set(), set(), ["x", "y", "z", "w", "v"], {"x", "y"}, set()),
    ]
    m = compute_metrics(outs)
    assert m["n_total"] == 4 and m["n_covered"] == 3
    assert m["coverage"] == 0.75 and m["useful_coverage"] == 0.5  # q1,q3 有有用卡 / 全 4 条(分母 n_total,codex R1)
    assert m["covered_useful_hit_at_5"] == 1 / 3   # 仅 q1
    assert m["global_useful_hit_at_5"] == 1 / 4
    assert m["conditional_hit"] == 2 / 3            # q1,q2 top5∩rel 非空
    assert m["conditional_useful"] == 1 / 2         # 命中的 q1,q2 中仅 q1 有有用卡
    assert m["useful_exists_rate"] == 2 / 3         # L1-useful-exists / L1-yes
    assert m["hit_at_1"] == 1 / 3 and m["hit_at_3"] == 2 / 3 and m["hit_at_5"] == 2 / 3
    assert math.isclose(m["mrr"], (1.0 + 1 / 3 + 0.0) / 3)
    assert math.isclose(m["precision_at_5"], (1 / 5 + 1 / 5 + 0 + 2 / 5) / 4)
    assert m["uncovered_pseudo_relevant_rate"] == 2 / 5  # q4 的 top5 相关占比(单查询均值)
    assert m["n_uncovered"] == 1
    assert m["uncovered_irrelevant_rate"] == 3 / 5
    assert m["go_with_guard"] is True  # 无关占比 3/5 = 0.6 ≥ 0.6 触发(spec §5;判无关不判相关)
