import pytest

from everos_probe import stats
from everos_probe.report import (
    COST_PER_CARD_THRESHOLD,
    PASS_RATE_THRESHOLD,
    CostSummary,
    FaithfulnessAudit,
    assemble_report,
    decide,
    funnel_breakdown,
    render_markdown,
)


def _clean_faith(n=12):
    return FaithfulnessAudit(cards_reviewed=n, fabricated_count=0)


def _red_faith(n=12):
    return FaithfulnessAudit(cards_reviewed=n, fabricated_count=1)


def _good_cost():
    return CostSummary(total_spend_usd=2.0, cards_generated=100)   # 0.02/card


def _bad_cost():
    return CostSummary(total_spend_usd=10.0, cards_generated=50)   # 0.20/card


def _boundary_cost():
    return CostSummary(total_spend_usd=5.0, cards_generated=100)   # 0.05/card == COST_PER_CARD_THRESHOLD


def test_decide_clear_go():
    v = decide(0.20, 0.35, _clean_faith(), _good_cost(), sample_incomplete=False, coverage_gap_strata=[])
    assert v["verdict"] == "clear GO"


def test_decide_clear_no_go_low_ci_upper():
    v = decide(0.02, 0.08, _clean_faith(), _good_cost(), sample_incomplete=False, coverage_gap_strata=[])
    assert v["verdict"] == "clear NO-GO"
    assert "CI upper" in v["reason"]


def test_decide_clear_no_go_faithfulness_red_flag_overrides_good_ci():
    v = decide(0.20, 0.35, _red_faith(), _good_cost(), sample_incomplete=False, coverage_gap_strata=[])
    assert v["verdict"] == "clear NO-GO"
    assert "red-flag" in v["reason"]


def test_decide_clear_no_go_cost_not_met():
    v = decide(0.20, 0.35, _clean_faith(), _bad_cost(), sample_incomplete=False, coverage_gap_strata=[])
    assert v["verdict"] == "clear NO-GO"
    assert "cost/card" in v["reason"]


def test_decide_marginal_when_ci_straddles_threshold():
    v = decide(0.10, 0.20, _clean_faith(), _good_cost(), sample_incomplete=False, coverage_gap_strata=[])
    assert v["verdict"] == "marginal"


def test_decide_forced_hold_on_sample_incomplete_even_with_great_numbers():
    v = decide(0.30, 0.40, _clean_faith(), _good_cost(), sample_incomplete=True, coverage_gap_strata=[])
    assert v["verdict"] == "HOLD"
    assert "cap-stop" in v["reason"]


def test_decide_forced_hold_on_coverage_gap_even_with_great_numbers():
    v = decide(0.30, 0.40, _clean_faith(), _good_cost(), sample_incomplete=False, coverage_gap_strata=["codex|6+"])
    assert v["verdict"] == "HOLD"
    assert v["coverage_gap_strata"] == ["codex|6+"]


def test_decide_none_safety_coverage_gap_short_circuits_before_ci_compare():
    """stats.compute_overall 的文档化不变式：ci_lower/ci_upper 为 None 当且仅当
    coverage_gap_strata 覆盖了全部有权重的层。decide() 必须先查 coverage_gap_strata
    再碰 ci_lower/ci_upper——若顺序反了，`None >= PASS_RATE_THRESHOLD` 会 TypeError。
    这里直接传 None 复现 compute_overall 的 ci_method="undefined" 分支，断言 decide()
    在不比较 None 的前提下仍正确落到 HOLD。"""
    v = decide(None, None, _clean_faith(), _good_cost(), sample_incomplete=False, coverage_gap_strata=["a", "b"])
    assert v["verdict"] == "HOLD"
    assert v["coverage_gap_strata"] == ["a", "b"]


def test_funnel_breakdown_counts_all_statuses_including_unobserved():
    outcomes = [
        {"stratum": "a", "status": "passed"},
        {"stratum": "a", "status": "passed"},
        {"stratum": "a", "status": "structural_reject"},
        {"stratum": "b", "status": "unobserved_cap"},
    ]
    assert funnel_breakdown(outcomes) == {"passed": 2, "structural_reject": 1, "unobserved_cap": 1}


def test_assemble_report_end_to_end_clear_go():
    outcomes = (
        [{"stratum": "a", "status": "passed"}] * 30
        + [{"stratum": "a", "status": "structural_reject"}] * 70   # n_a=100, rate=0.3
        + [{"stratum": "b", "status": "passed"}] * 20
        + [{"stratum": "b", "status": "structural_reject"}] * 80   # n_b=100, rate=0.2
    )
    w_raw = {"a": 0.5, "b": 0.5}
    report = assemble_report(outcomes, w_raw, _clean_faith(), _good_cost(), sample_incomplete=False)
    assert report["overall"]["weighted_pass_rate"] == pytest.approx(0.25)
    assert report["verdict"]["verdict"] == "clear GO"
    assert "## 过门率" in render_markdown(report)


def test_assemble_report_hold_when_stratum_never_fed():
    outcomes = [{"stratum": "a", "status": "passed"}] * 5 + [{"stratum": "a", "status": "structural_reject"}] * 5
    w_raw = {"a": 0.5, "b": 0.5}   # b 从未被喂到
    report = assemble_report(outcomes, w_raw, _clean_faith(), _good_cost(), sample_incomplete=False)
    assert report["verdict"]["verdict"] == "HOLD"
    assert report["verdict"]["coverage_gap_strata"] == ["b"]


def test_assemble_report_hold_when_all_sessions_unobserved():
    # 全部会话都是未观测(cap 提前停 / 坏样本剔除)：stats.compute_overall 内部会撞见
    # reweight_for_zero_observed 的全零观测 raise。assemble_report 必须不崩溃、返回一份
    # HOLD 结论 + 完整 artifact(不是让异常冒出来炸调用方)。
    outcomes = [
        {"stratum": "a", "status": "unobserved_cap"},
        {"stratum": "b", "status": "unobserved_excluded"},
    ]
    w_raw = {"a": 0.6, "b": 0.4}
    report = assemble_report(outcomes, w_raw, _clean_faith(), _good_cost(), sample_incomplete=False)
    assert report["verdict"]["verdict"] == "HOLD"
    assert set(report["verdict"]["coverage_gap_strata"]) == {"a", "b"}


# --- C1: render_markdown 在 all-unobserved(HOLD) report 上必须不崩(None-safety) ---


def test_render_markdown_handles_all_unobserved_hold_report_without_crashing():
    """assemble_report_hold_when_all_sessions_unobserved 的 report 里
    overall.weighted_pass_rate/ci_lower/ci_upper 全是 None（stats.compute_overall
    的 ci_method="undefined" 分支）。§11 要求 HOLD 结论仍要落盘报告，不能在渲染这步
    崩溃。"""
    outcomes = [
        {"stratum": "a", "status": "unobserved_cap"},
        {"stratum": "b", "status": "unobserved_excluded"},
    ]
    w_raw = {"a": 0.6, "b": 0.4}
    report = assemble_report(outcomes, w_raw, _clean_faith(), _good_cost(), sample_incomplete=False)
    assert report["overall"]["weighted_pass_rate"] is None
    assert report["overall"]["ci_lower"] is None
    assert report["overall"]["ci_upper"] is None

    md = render_markdown(report)  # 必须不抛 TypeError

    assert "n/a" in md
    assert "HOLD" in md


# --- F1: marginal reason 不得泄漏真实人名(PUBLIC 仓 reports/ 落盘隐私) ---


def test_decide_marginal_reason_has_no_real_name_leak():
    v = decide(0.10, 0.20, _clean_faith(), _good_cost(), sample_incomplete=False, coverage_gap_strata=[])
    assert v["verdict"] == "marginal"
    assert "Ivan" not in v["reason"]


# --- F2: 0.15 决策边界的包含性回归(sonnet+opus 对抗审：mutation >= -> > 时静默漏保护) ---


def test_decide_ci_lower_at_threshold_is_clear_go():
    """ci_lower == PASS_RATE_THRESHOLD 含边界，走 clear GO（decide() 用 >=）。"""
    v = decide(
        PASS_RATE_THRESHOLD, 0.30, _clean_faith(), _good_cost(),
        sample_incomplete=False, coverage_gap_strata=[],
    )
    assert v["verdict"] == "clear GO"


def test_decide_ci_upper_at_threshold_is_marginal_not_no_go():
    """ci_upper == PASS_RATE_THRESHOLD 不满足 `< PASS_RATE_THRESHOLD`，NO-GO 分支不触发，
    应落到 marginal（decide() 的 NO-GO 判据是严格小于）。"""
    v = decide(
        0.05, PASS_RATE_THRESHOLD, _clean_faith(), _good_cost(),
        sample_incomplete=False, coverage_gap_strata=[],
    )
    assert v["verdict"] == "marginal"
    assert v["verdict"] != "clear NO-GO"


def test_decide_cost_per_card_at_threshold_is_go_side():
    """cost_per_card == COST_PER_CARD_THRESHOLD 含边界（decide() 用 <=），落 GO 侧。"""
    v = decide(
        0.20, 0.35, _clean_faith(), _boundary_cost(),
        sample_incomplete=False, coverage_gap_strata=[],
    )
    assert v["verdict"] == "clear GO"


# --- F3: decide()/assemble_report() 对全零正权重层(degenerate w_raw)的 None-safety 兜底 ---
# opus 实测：stats.compute_overall 在“零正权重层”输入下返回 ci_lower=None 但
# coverage_gap_strata=[]（该字段只收 wᵢ>0 且 nᵢ=0 的层，没有正权重层就收不到东西）。
# decide() 若只靠 coverage_gap_strata 短路会撞 `None >= PASS_RATE_THRESHOLD` TypeError。
# 生产环境不现实（真实 CASS 库不会全零占比），但必须 fail-safe 成 HOLD 而不是 cryptic 崩溃。


def test_assemble_report_empty_outcomes_and_empty_w_raw_holds_without_crashing():
    report = assemble_report([], {}, _clean_faith(), _good_cost(), sample_incomplete=False)
    assert report["verdict"]["verdict"] == "HOLD"


def test_compute_overall_all_zero_weight_stratum_decide_holds_without_crashing():
    overall = stats.compute_overall([], {"a": 0.0})
    assert overall["ci_lower"] is None
    assert overall["coverage_gap_strata"] == []  # vacuous：没有正权重层可收进缺口列表

    v = decide(
        overall["ci_lower"], overall["ci_upper"], _clean_faith(), _good_cost(),
        sample_incomplete=False, coverage_gap_strata=overall["coverage_gap_strata"],
    )
    assert v["verdict"] == "HOLD"
