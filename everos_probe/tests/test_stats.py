import math

import pytest

from everos_probe import stats


def test_aggregate_fed_outcomes_counts_only_fed_statuses():
    outcomes = [
        {"stratum": "a", "status": "passed"},
        {"stratum": "a", "status": "structural_reject"},
        {"stratum": "a", "status": "unobserved_cap"},
        {"stratum": "b", "status": "semantic_reject"},
        {"stratum": "b", "status": "other"},
        {"stratum": "b", "status": "unobserved_excluded"},
    ]
    n, k = stats.aggregate_fed_outcomes(outcomes)
    assert n == {"a": 2, "b": 2}
    # k 只在实际发生过 passed 时才建键（defaultdict -> dict 的忠实转换，非稀疏填 0）；
    # 下游 compute_overall 用 `k.get(s, 0)` 消费，缺键即 0，语义等价。断言按此实际形状写，
    # 不是 {"a": 1, "b": 0}（那样断言在原实现上恒假——发现于本次验证跑测）。
    assert k == {"a": 1}


def test_weighted_pass_rate_matches_hand_formula():
    w = {"a": 0.6, "b": 0.4}
    k = {"a": 6, "b": 2}
    n = {"a": 10, "b": 10}
    expected = 0.6 * (6 / 10) + 0.4 * (2 / 10)
    assert stats.weighted_pass_rate(w, k, n) == pytest.approx(expected)


def test_stratified_variance_matches_hand_formula():
    w = {"a": 0.6, "b": 0.4}
    k = {"a": 6, "b": 2}
    n = {"a": 10, "b": 10}
    pa, pb = 6 / 10, 2 / 10
    expected = (0.6 ** 2) * pa * (1 - pa) / 10 + (0.4 ** 2) * pb * (1 - pb) / 10
    assert stats.stratified_variance(w, k, n) == pytest.approx(expected)


def test_weighted_ci_matches_hand_formula_band():
    w = {"a": 0.6, "b": 0.4}
    k = {"a": 6, "b": 2}
    n = {"a": 10, "b": 10}
    p_hat, lo, hi = stats.weighted_ci(w, k, n)
    pa, pb = 0.6, 0.2
    expected_p = 0.6 * pa + 0.4 * pb
    expected_var = (0.6 ** 2) * pa * (1 - pa) / 10 + (0.4 ** 2) * pb * (1 - pb) / 10
    expected_half = 1.96 * math.sqrt(expected_var)
    assert p_hat == pytest.approx(expected_p)
    assert lo == pytest.approx(expected_p - expected_half)
    assert hi == pytest.approx(expected_p + expected_half)


def test_weighted_ci_clamped_to_zero_one():
    # n=2,k=1 -> p_hat=0.5(最大方差点)，Var=0.5*0.5/2=0.125，raw half=1.96*sqrt(0.125)≈0.693
    # -> raw band [-0.193, 1.193] 两端都越界，必须被 clamp 拉回 [0,1]（原用例 p_hat=1→Var=0
    # 从未越界，从未真正跑到 clamp 分支，是假阳性）。
    w = {"a": 1.0}
    k = {"a": 1}
    n = {"a": 2}
    p_hat, lo, hi = stats.weighted_ci(w, k, n)
    assert p_hat == pytest.approx(0.5)
    assert lo == 0.0
    assert hi == 1.0


def test_reweight_redistributes_zero_observed_stratum_proportionally():
    w = {"a": 0.5, "b": 0.3, "c": 0.2}
    n = {"a": 10, "b": 0, "c": 8}
    out = stats.reweight_for_zero_observed(w, n)
    denom = 0.5 + 0.2
    assert out["a"] == pytest.approx(0.5 / denom)
    assert out["c"] == pytest.approx(0.2 / denom)
    assert out["b"] == 0.0
    assert out["a"] + out["b"] + out["c"] == pytest.approx(1.0)


def test_reweight_all_unobserved_raises():
    with pytest.raises(ValueError):
        stats.reweight_for_zero_observed({"a": 1.0}, {"a": 0})


def test_coverage_gap_strata_excludes_true_zero_weight_cells():
    w = {"a": 0.5, "b": 0.0, "c": 0.5}   # b：库中本就不存在(wᵢ=0)，不是缺口
    n = {"a": 10, "b": 0, "c": 0}        # c：wᵢ>0 但 nᵢ=0，真缺口
    assert stats.coverage_gap_strata(w, n) == ["c"]


def test_wilson_ci_matches_hand_formula():
    k_, n_, z = 8, 10, 1.96
    p = k_ / n_
    denom = 1 + (z * z) / n_
    center = p + (z * z) / (2 * n_)
    adj = z * math.sqrt(p * (1 - p) / n_ + (z * z) / (4 * n_ * n_))
    expected = ((center - adj) / denom, (center + adj) / denom)
    assert stats.wilson_ci(k_, n_) == pytest.approx(expected)


def test_wilson_ci_rejects_zero_n():
    with pytest.raises(ValueError):
        stats.wilson_ci(0, 0)


def test_should_bootstrap_triggers_on_small_observed_stratum():
    assert stats.should_bootstrap({"a": 3, "b": 20}, floor=5) is True
    assert stats.should_bootstrap({"a": 5, "b": 20}, floor=5) is False
    assert stats.should_bootstrap({"a": 0, "b": 20}, floor=5) is False   # 未观测≠小样本，算覆盖缺口


def test_bootstrap_ci_reproducible_with_fixed_seed():
    sessions = {"a": [1, 1, 0], "b": [1, 0, 0, 0, 0]}
    w = {"a": 0.5, "b": 0.5}
    lo1, hi1 = stats.bootstrap_ci(sessions, w)
    lo2, hi2 = stats.bootstrap_ci(sessions, w)
    assert (lo1, hi1) == (lo2, hi2)
    assert 0.0 <= lo1 <= hi1 <= 1.0


def test_compute_overall_reports_coverage_gap_without_silently_dropping_it():
    outcomes = (
        [{"stratum": "a", "status": "passed"}] * 3
        + [{"stratum": "a", "status": "structural_reject"}] * 3
    )
    w_raw = {"a": 0.7, "b": 0.3}   # b 层一个都没喂到
    out = stats.compute_overall(outcomes, w_raw, floor=5)
    assert out["coverage_gap_strata"] == ["b"]
    assert out["w_reweighted"]["a"] == pytest.approx(1.0)
    assert out["w_reweighted"]["b"] == 0.0
    assert out["weighted_pass_rate"] == pytest.approx(3 / 6)
    assert out["ci_method"] == "analytic"   # n_a=6 >= floor(5)，不触发 bootstrap


def test_compute_overall_falls_back_to_bootstrap_for_small_stratum():
    outcomes = (
        [{"stratum": "a", "status": "passed"}] * 2
        + [{"stratum": "a", "status": "structural_reject"}] * 2     # n_a = 4 < floor(5)
        + [{"stratum": "b", "status": "passed"}] * 10
        + [{"stratum": "b", "status": "structural_reject"}] * 10    # n_b = 20
    )
    w_raw = {"a": 0.5, "b": 0.5}
    out = stats.compute_overall(outcomes, w_raw, floor=5)
    assert out["ci_method"] == "bootstrap"
    assert out["ci_lower"] <= out["weighted_pass_rate"] <= out["ci_upper"]


def test_aggregate_fed_outcomes_raises_on_unrecognized_status():
    # 未知/拼写错的 status（既不在 FED_STATUSES 也不在 UNOBSERVED_STATUSES）曾被静默跳过
    # ——喂料层的一个 typo 就会让样本悄悄消失而不报错。必须 fail-loud。
    outcomes = [{"stratum": "a", "status": "pased"}]
    with pytest.raises(ValueError):
        stats.aggregate_fed_outcomes(outcomes)


def test_compute_overall_raises_on_stratum_label_not_in_w_raw():
    # compute_overall 曾用 `{s: ... for s in w_raw}` 重新按 w_raw 的键取数，任何喂进来的
    # outcome 若 stratum 字符串跟 w_raw 的键对不上会被悄悄丢弃、不报错。必须 fail-loud，
    # 跟 status 字符串同等纪律。
    outcomes = [{"stratum": "claude_code|typo-bucket", "status": "passed"}]
    w_raw = {"claude_code|<3": 1.0}
    with pytest.raises(ValueError):
        stats.compute_overall(outcomes, w_raw, floor=5)


def test_compute_overall_all_unobserved_does_not_raise_and_flags_full_coverage_gap():
    # 全部层都 0 观测（例如 cap 提前停在第一个会话之前 / 全部坏样本剔除）：
    # reweight_for_zero_observed 会 raise（其契约本身如此），但 compute_overall 必须接住，
    # 不崩溃——把"未定义"结构化透传给 report 层，由非空 coverage_gap_strata 强制 HOLD（§11）。
    outcomes = [
        {"stratum": "a", "status": "unobserved_cap"},
        {"stratum": "a", "status": "unobserved_excluded"},
        {"stratum": "b", "status": "unobserved_cap"},
    ]
    w_raw = {"a": 0.6, "b": 0.4}
    out = stats.compute_overall(outcomes, w_raw, floor=5)   # 不得抛异常
    assert out["coverage_gap_strata"] == ["a", "b"]
    assert out["weighted_pass_rate"] is None
    assert out["ci_method"] == "undefined"


def test_bootstrap_ci_degenerate_all_pass_pins_to_one():
    # 已知答案的退化用例：单层、全部会话 pass -> 每次放回重抽样的结果恒为全 1，
    # 加权率恒为 1.0，95% CI 应精确收敛到 [1.0, 1.0]。错误实现（如百分位算反/抽样逻辑错）
    # 在这个已知答案上会失配，而不仅仅是"跑两次结果一样"这种空心的确定性检查。
    sessions = {"a": [1] * 10}
    w = {"a": 1.0}
    lo, hi = stats.bootstrap_ci(sessions, w)
    assert lo == pytest.approx(1.0)
    assert hi == pytest.approx(1.0)


def test_bootstrap_ci_degenerate_all_fail_pins_to_zero():
    sessions = {"a": [0] * 10}
    w = {"a": 1.0}
    lo, hi = stats.bootstrap_ci(sessions, w)
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(0.0)
