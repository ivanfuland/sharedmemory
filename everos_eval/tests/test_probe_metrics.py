"""probe_metrics.py 的测试(P5 §Task 6:判据引擎 —— 三 floor + maximin + group-LOO/
最终拟合分离 + Layer 2 字典序 + 误差方向标签)。微世界手造,不接触任何真实分数。
"""
from __future__ import annotations

import pytest

from everos_eval.probe_arms import ARMS, ScoredQuery
from everos_eval.probe_metrics import (
    apply_fixed_fold_thetas,
    arm_theta_grid,
    baseline_macro_fdr,
    check_floors_pass,
    compute_layer1_floors,
    compute_returned,
    direction_stability,
    enumerate_theta_candidates,
    error_direction_label,
    final_fit,
    fit_threshold,
    grouped_loocv,
    layer2_select,
    merge_interleave,
    summarize_oof_for_layer2,
    survives,
    transport_check,
)


# ======================================================================
# merge_interleave / compute_returned:返回集唯一操作化(冻结公式)
# ======================================================================

def _card(cid, mem_type, rank):
    return {"canonical_card_id": cid, "mem_type": mem_type, "source_rank": rank}


def test_merge_interleave_is_skill_first_alternating():
    cases = [_card("ac_0", "agent_case", 0), _card("ac_1", "agent_case", 1)]
    skills = [_card("sk_0", "agent_skill", 0)]
    order = merge_interleave(cases, skills, k=3)
    assert [c["canonical_card_id"] for c in order] == ["sk_0", "ac_0", "ac_1"]


def test_compute_returned_20plus20_tail_item_not_truncated_away():
    # 冻结回归:20 case + 20 skill,唯一 allowed 是尾部第 40 张(交错序最后一个 = 最后一张 case)。
    cases = [_card(f"ac_{i}", "agent_case", i) for i in range(20)]
    skills = [_card(f"sk_{i}", "agent_skill", i) for i in range(20)]
    returned = compute_returned(cases, skills, allowed={"ac_19"}, limit=5)
    assert returned == [cases[19]]


def test_compute_returned_missing_side_rank1_still_interleaves_correctly():
    # 删掉一侧 rank1(只留 case 的第二张,rank0 缺失)
    cases = [_card("ac_2", "agent_case", 1)]
    skills = [_card("sk_1", "agent_skill", 0)]
    returned = compute_returned(cases, skills, allowed={"ac_2", "sk_1"}, limit=5)
    assert returned == [skills[0], cases[0]]  # skill 先,序不受一侧缺失影响


def test_compute_returned_truncates_when_more_than_five_allowed():
    cases = [_card(f"ac_{i}", "agent_case", i) for i in range(6)]
    allowed = {c["canonical_card_id"] for c in cases}
    returned = compute_returned(cases, [], allowed=allowed, limit=5)
    assert [c["canonical_card_id"] for c in returned] == ["ac_0", "ac_1", "ac_2", "ac_3", "ac_4"]


def test_compute_returned_card_at_position_six_after_filter_is_dropped():
    cases = [_card(f"ac_{i}", "agent_case", i) for i in range(6)]
    allowed = {c["canonical_card_id"] for c in cases}
    returned = compute_returned(cases, [], allowed=allowed, limit=5)
    assert "ac_5" not in {c["canonical_card_id"] for c in returned}


def test_compute_returned_accepts_callable_predicate_matching_frozen_formula():
    # 冻结公式字面是 `if allowed(x)`——callable 谓词形式必须同样支持,不只是 set 包装。
    cases = [_card("ac_0", "agent_case", 0)]
    predicate = lambda x: x["canonical_card_id"] == "ac_0"  # noqa: E731
    returned = compute_returned(cases, [], allowed=predicate, limit=5)
    assert returned == cases


# ======================================================================
# enumerate_theta_candidates:相邻去重区间中点,不落在实测分值本身上
# ======================================================================

def test_enumerate_theta_candidates_midpoints_never_equal_observed_values():
    vals = [1.0, 2.0, 2.0, 5.0]
    grid = enumerate_theta_candidates(vals)
    assert grid == [0.0, 1.5, 3.5, 6.0]
    assert all(g not in set(vals) for g in grid)


def _four_candidate_sq(qid="qz"):
    candidates = (
        {"canonical_card_id": "ac_1", "mem_type": "agent_case", "source_rank": 0,
         "native_score": 5.0, "cos": 0.5, "ce": 2.0},
        {"canonical_card_id": "ac_2", "mem_type": "agent_case", "source_rank": 1,
         "native_score": 3.0, "cos": 0.3, "ce": 1.0},
        {"canonical_card_id": "sk_1", "mem_type": "agent_skill", "source_rank": 0,
         "native_score": 9.0, "cos": 0.8, "ce": 4.0},
        {"canonical_card_id": "sk_2", "mem_type": "agent_skill", "source_rank": 1,
         "native_score": 7.0, "cos": 0.6, "ce": 3.0},
    )
    decoy_ce_by_type = {"agent_case": (1.5, 1.0), "agent_skill": (3.5, 2.0)}
    return ScoredQuery(query_id=qid, candidates=candidates, decoy_ce_by_type=decoy_ce_by_type)


@pytest.mark.parametrize("arm_name", list(ARMS))
def test_arm_theta_grid_builds_a_nonempty_grid_for_every_arm(arm_name):
    # 回归门:每个臂的 θ 网格枚举都必须真的跑通(曾经 ce_znorm 因为对 ces 里的
    # float 又当 dict 取 ["ce"] 崩过,微世界只测过 fit_threshold 本身、没测过
    # arm_theta_grid 的信号取值路径,这条漏网)。
    grid = arm_theta_grid([_four_candidate_sq()], arm_name)
    assert len(grid) > 0
    for theta in grid:
        allowed = ARMS[arm_name].apply(_four_candidate_sq(), theta)
        assert isinstance(allowed, set)


# ======================================================================
# fit_threshold:maximin + FDR 回归 + 字典序平局
# ======================================================================

def test_fit_threshold_lower_fdr_point_never_loses_when_other_slacks_tie():
    """回归写死(P5):其余相等时,FDR 更低的可行点绝不输给更高者。"""
    floors_by_theta = {
        1.0: {"abstain_rate": 0.90, "useful_rate": 0.95, "macro_fdr": 0.19, "conditional_fdr": 0.19},
        2.0: {"abstain_rate": 0.90, "useful_rate": 0.95, "macro_fdr": 0.05, "conditional_fdr": 0.05},
    }
    theta = fit_threshold(list(floors_by_theta), lambda t: floors_by_theta[t],
                           contamination_floor=0.20)
    assert theta == 2.0


def test_fit_threshold_tie_break_picks_larger_scalar_theta():
    floors = {"abstain_rate": 0.90, "useful_rate": 0.95, "macro_fdr": 0.05, "conditional_fdr": 0.05}
    theta = fit_threshold([1.0, 5.0, 3.0], lambda t: floors, contamination_floor=0.20)
    assert theta == 5.0


def test_fit_threshold_tie_break_lexicographic_for_2d_theta():
    floors = {"abstain_rate": 0.90, "useful_rate": 0.95, "macro_fdr": 0.05, "conditional_fdr": 0.05}
    grid = [(1.0, 9.0), (2.0, 1.0), (2.0, 5.0)]
    theta = fit_threshold(grid, lambda t: floors, contamination_floor=0.20)
    assert theta == (2.0, 5.0)


def test_fit_threshold_empty_feasible_set_returns_none():
    floors = {"abstain_rate": 0.10, "useful_rate": 0.10, "macro_fdr": 0.90, "conditional_fdr": 0.90}
    theta = fit_threshold([1.0, 2.0, 3.0], lambda t: floors, contamination_floor=0.20)
    assert theta is None


def test_fit_threshold_reasserts_chosen_theta_before_returning():
    # evaluate_fn 精确重算(不做区间/单调假设)——theta 选出后应仍能通过重断言。
    calls = []

    def evaluate_fn(theta):
        calls.append(theta)
        return {"abstain_rate": 0.9, "useful_rate": 0.9, "macro_fdr": 0.1, "conditional_fdr": 0.1}

    theta = fit_threshold([1.0], evaluate_fn, contamination_floor=0.5)
    assert theta == 1.0
    assert calls.count(1.0) >= 2  # 至少一次挑选 + 一次重断言


# ======================================================================
# grouped_loocv:留出组不泄漏 + 同组同进同出
# ======================================================================

def _sq(qid, case_cos, skill_cos, case_id="ac_1", skill_id="sk_1"):
    candidates = (
        {"canonical_card_id": case_id, "mem_type": "agent_case", "source_rank": 0, "cos": case_cos},
        {"canonical_card_id": skill_id, "mem_type": "agent_skill", "source_rank": 0, "cos": skill_cos},
    )
    return ScoredQuery(query_id=qid, candidates=candidates, decoy_ce_by_type={})


def _four_query_world():
    sq_by_qid = {
        "q1": _sq("q1", 0.9, 0.1),
        "q2": _sq("q2", 0.8, 0.2),
        "q3": _sq("q3", 0.7, 0.3),
        "q4": _sq("q4", 0.6, 0.4),
    }
    gold_variant = {
        "labels": {
            ("q1", "ac_1"): {"relevant": True, "useful": True},
            ("q1", "sk_1"): {"relevant": False, "useful": False},
            ("q2", "ac_1"): {"relevant": True, "useful": True},
            ("q2", "sk_1"): {"relevant": False, "useful": False},
            ("q3", "ac_1"): {"relevant": True, "useful": True},
            ("q3", "sk_1"): {"relevant": False, "useful": False},
            ("q4", "ac_1"): {"relevant": True, "useful": True},
            ("q4", "sk_1"): {"relevant": False, "useful": False},
        },
        "covered": {"q1", "q2", "q3", "q4"},
        "uncovered": set(),
        "baseline_useful": {"q1", "q2", "q3", "q4"},
        "excluded": frozenset(),
        "groups": {"G1": ["q1", "q2"], "G2": ["q3", "q4"]},
    }
    return sq_by_qid, gold_variant


def test_grouped_loocv_same_group_queries_always_held_out_together():
    sq_by_qid, gold_variant = _four_query_world()
    result = grouped_loocv(sq_by_qid, ARMS["cos_unified"], gold_variant, contamination_floor=1.0)
    for group_key, qids in gold_variant["groups"].items():
        train_qids = set(result["fold_train_qids"][group_key])
        for qid in qids:
            assert qid not in train_qids  # 组内查询整体被留出,不会自己训自己


def test_grouped_loocv_holdout_group_own_score_change_does_not_change_its_fold_theta():
    """留出组信息不泄漏:该组自己的分数变了,不该改变它自己那一折的 train-θ
    (train-θ 只用其余组数据拟合,本就看不到留出组的数据)。"""
    sq_by_qid, gold_variant = _four_query_world()
    contamination_floor = 1.0

    result_before = grouped_loocv(sq_by_qid, ARMS["cos_unified"], gold_variant, contamination_floor)
    theta_g2_before = result_before["fold_thetas"]["G2"]
    assert theta_g2_before is not None

    perturbed = dict(sq_by_qid)
    perturbed["q3"] = _sq("q3", 0.001, 0.002)  # 剧烈改动 q3(G2 成员)自己的分数
    result_after = grouped_loocv(perturbed, ARMS["cos_unified"], gold_variant, contamination_floor)
    theta_g2_after = result_after["fold_thetas"]["G2"]

    assert theta_g2_after == theta_g2_before


def test_grouped_loocv_and_final_fit_survive_together_positive_case():
    sq_by_qid, gold_variant = _four_query_world()
    contamination_floor = 1.0

    cv = grouped_loocv(sq_by_qid, ARMS["cos_unified"], gold_variant, contamination_floor)
    fin = final_fit(sq_by_qid, ARMS["cos_unified"], gold_variant, contamination_floor)

    assert cv["cv_pass"] is True
    assert fin["feasible"] is True
    assert survives(cv, fin) is True


def test_grouped_loocv_and_final_fit_abstain_all_when_floor_impossible():
    sq_by_qid = {"q1": _sq("q1", 0.9, 0.1)}
    gold_variant = {
        "labels": {("q1", "ac_1"): {"relevant": True, "useful": True}},
        "covered": {"q1"}, "uncovered": set(), "baseline_useful": {"q1"},
        "excluded": frozenset(), "groups": {"g": ["q1"]},
    }
    # floor=-1.0 让 s3/s3b 恒负,任何 theta 都不可行 -> abstain-all
    cv = grouped_loocv(sq_by_qid, ARMS["cos_unified"], gold_variant, contamination_floor=-1.0)
    fin = final_fit(sq_by_qid, ARMS["cos_unified"], gold_variant, contamination_floor=-1.0)

    assert cv["fold_thetas"]["g"] is None
    assert cv["oof_returned"]["q1"] == []
    assert cv["cv_pass"] is False
    assert fin == {"theta_star": None, "resub_floors": None, "feasible": False}
    assert survives(cv, fin) is False


def test_apply_fixed_fold_thetas_reuses_theta_without_refitting():
    sq_by_qid, gold_variant = _four_query_world()
    cv = grouped_loocv(sq_by_qid, ARMS["cos_unified"], gold_variant, contamination_floor=1.0)

    # 换一份 score matrix(候选分数不同),但复用同一份 fold_thetas——不重新拟合。
    other_sq_by_qid = {
        "q1": _sq("q1", 0.95, 0.05),
        "q2": _sq("q2", 0.85, 0.15),
        "q3": _sq("q3", 0.75, 0.25),
        "q4": _sq("q4", 0.65, 0.35),
    }
    returned = apply_fixed_fold_thetas(other_sq_by_qid, ARMS["cos_unified"], gold_variant,
                                        cv["fold_thetas"])
    assert set(returned) == {"q1", "q2", "q3", "q4"}
    # 复用的是同一批 fold theta(未重新拟合),结果必须是该 theta 在新分数矩阵上的确定性重算
    for qid, sq in other_sq_by_qid.items():
        group = next(g for g, qids in gold_variant["groups"].items() if qid in qids)
        theta = cv["fold_thetas"][group]
        expected_allowed = set() if theta is None else ARMS["cos_unified"].apply(sq, theta)
        assert {c["canonical_card_id"] for c in returned[qid]} == expected_allowed


# ======================================================================
# 剔除卡三性质(primary 计分)
# ======================================================================

class _FixedArm:
    """测试用假 arm:不做阈值判定,直接按 query_id 返回预设放行集合。"""
    name = "fixed"

    def __init__(self, allowed_by_qid):
        self._allowed_by_qid = allowed_by_qid

    def apply(self, sq, theta):
        return self._allowed_by_qid.get(sq.query_id, set())


def _fixed_sq(qid, cids):
    candidates = tuple(
        {"canonical_card_id": cid, "mem_type": "agent_case", "source_rank": i}
        for i, cid in enumerate(cids)
    )
    return ScoredQuery(query_id=qid, candidates=candidates, decoy_ce_by_type={})


def test_excluded_card_breaks_abstain():
    sq_list = [_fixed_sq("q1", ["ex_1"])]
    arm = _FixedArm({"q1": {"ex_1"}})
    gold_variant = {
        "labels": {},  # ex_1 是剔除卡,primary 缺席
        "covered": set(), "uncovered": {"q1"}, "baseline_useful": set(),
        "excluded": frozenset({("q1", "ex_1")}), "groups": {},
    }
    floors = compute_layer1_floors(sq_list, arm, theta="x", gold_variant=gold_variant)
    assert floors["abstain_rate"] == 0.0  # returned 非空(剔除卡本身放行)-> 不算 abstain
    assert floors["per_query"]["q1"]["abstain"] is False


def test_excluded_card_does_not_count_as_useful():
    sq_list = [_fixed_sq("q1", ["ex_1"])]
    arm = _FixedArm({"q1": {"ex_1"}})
    gold_variant = {
        "labels": {},  # primary:剔除卡不出现在 labels 里,不算 useful
        "covered": {"q1"}, "uncovered": set(), "baseline_useful": {"q1"},
        "excluded": frozenset({("q1", "ex_1")}), "groups": {},
    }
    floors = compute_layer1_floors(sq_list, arm, theta="x", gold_variant=gold_variant)
    assert floors["useful_rate"] == 0.0
    assert floors["per_query"]["q1"]["useful_hit"] is False


def test_excluded_card_counted_as_irrelevant_in_fdr_numerator_and_denominator():
    sq_list = [_fixed_sq("q1", ["ex_1", "rel_1"])]
    arm = _FixedArm({"q1": {"ex_1", "rel_1"}})
    gold_variant = {
        "labels": {("q1", "rel_1"): {"relevant": True, "useful": True}},  # ex_1 缺席 -> irrelevant
        "covered": {"q1"}, "uncovered": set(), "baseline_useful": set(),
        "excluded": frozenset({("q1", "ex_1")}), "groups": {},
    }
    floors = compute_layer1_floors(sq_list, arm, theta="x", gold_variant=gold_variant)
    assert floors["macro_fdr"] == pytest.approx(0.5)  # 1 irrelevant(ex_1) / 2 returned
    assert floors["per_query"]["q1"]["fdr"] == pytest.approx(0.5)


def test_returned_all_excluded_cards_forces_fdr_one_even_under_optimistic_labels():
    sq_list = [_fixed_sq("q1", ["ex_1", "ex_2"])]
    arm = _FixedArm({"q1": {"ex_1", "ex_2"}})
    gold_variant = {
        # 模拟 sens_rel 的乐观翻转(剔除卡被标 relevant=True)——override 必须仍强制 FDR=1
        "labels": {
            ("q1", "ex_1"): {"relevant": True, "useful": True},
            ("q1", "ex_2"): {"relevant": True, "useful": True},
        },
        "covered": {"q1"}, "uncovered": set(), "baseline_useful": set(),
        "excluded": frozenset({("q1", "ex_1"), ("q1", "ex_2")}), "groups": {},
    }
    floors = compute_layer1_floors(sq_list, arm, theta="x", gold_variant=gold_variant)
    assert floors["macro_fdr"] == 1.0
    assert floors["conditional_fdr"] == 1.0


def test_empty_returned_covered_query_has_fdr_zero():
    sq_list = [_fixed_sq("q1", [])]
    arm = _FixedArm({"q1": set()})
    gold_variant = {
        "labels": {}, "covered": {"q1"}, "uncovered": set(), "baseline_useful": set(),
        "excluded": frozenset(), "groups": {},
    }
    floors = compute_layer1_floors(sq_list, arm, theta="x", gold_variant=gold_variant)
    assert floors["macro_fdr"] == 0.0
    assert floors["conditional_fdr"] == 0.0  # 空放行不计入条件 FDR 的分母(不是 0/0 的人为拉低)


def test_check_floors_pass_boundary_equal_to_floor_passes():
    floors = {"abstain_rate": 0.80, "useful_rate": 0.85, "macro_fdr": 0.25, "conditional_fdr": 0.25}
    assert check_floors_pass(floors, contamination_floor=0.25) is True


# ======================================================================
# transport_check:θ 运输门失败路径
# ======================================================================

def test_transport_check_fails_when_theta_does_not_transport():
    sq_by_qid = {"q1": _sq("q1", case_cos=0.1, skill_cos=0.05)}
    gold_variant = {
        "labels": {("q1", "ac_1"): {"relevant": True, "useful": True}},
        "covered": {"q1"}, "uncovered": set(), "baseline_useful": {"q1"},
        "excluded": frozenset(), "groups": {"g": ["q1"]},
    }
    result = transport_check(theta_star=0.99, arm=ARMS["cos_unified"], sq_by_qid=sq_by_qid,
                              gold_variant=gold_variant, contamination_floor=1.0)
    assert result["passed"] is False
    assert result["floors"]["useful_rate"] == 0.0


def test_transport_check_passes_when_theta_transports_cleanly():
    sq_by_qid = {"q1": _sq("q1", case_cos=0.9, skill_cos=0.1)}
    gold_variant = {
        "labels": {("q1", "ac_1"): {"relevant": True, "useful": True}},
        "covered": {"q1"}, "uncovered": set(), "baseline_useful": {"q1"},
        "excluded": frozenset(), "groups": {"g": ["q1"]},
    }
    result = transport_check(theta_star=0.5, arm=ARMS["cos_unified"], sq_by_qid=sq_by_qid,
                              gold_variant=gold_variant, contamination_floor=1.0)
    assert result["passed"] is True


# ======================================================================
# 误差方向标签(三分支)
# ======================================================================

def test_error_direction_label_three_branches():
    assert error_direction_label(kill=5, leak=2) == "kill-heavy"
    assert error_direction_label(kill=1, leak=4) == "leak-heavy"
    assert error_direction_label(kill=3, leak=3) == "balanced"


def test_direction_stability_detects_flip_across_gold_variants():
    """P1-12 方向稳定性:同一份 OOF 预测在某个 sens 变体下误差方向翻转 →
    direction_stable 必须为 False,标签逐变体记录。"""
    oof_returned = {
        "u1": [_card("x1", "agent_case", 0)],   # primary:uncovered 放行 → leak=1
        "b1": [_card("c1", "agent_case", 0)],
    }
    primary = {
        "labels": {("b1", "c1"): {"relevant": True, "useful": True}},   # B 命中 → kill=0
        "uncovered": {"u1"}, "baseline_useful": {"b1"}, "covered": {"b1"},
    }
    # sens_rel:u1 翻成 covered(不再算 leak),b1 的卡 useful 翻成 False → kill=1
    sens_flipped = {
        "labels": {("b1", "c1"): {"relevant": True, "useful": False},
                    ("u1", "x1"): {"relevant": True, "useful": False}},
        "uncovered": set(), "baseline_useful": {"b1"}, "covered": {"b1", "u1"},
    }
    gold = {"primary": primary, "sens_rel": sens_flipped, "sens_irr": primary}

    result = direction_stability(oof_returned, gold)
    assert result["labels"]["primary"] == "leak-heavy"
    assert result["labels"]["sens_rel"] == "kill-heavy"
    assert result["labels"]["sens_irr"] == "leak-heavy"
    assert result["direction_stable"] is False


def test_direction_stability_true_when_all_variants_agree():
    oof_returned = {"u1": [_card("x1", "agent_case", 0)]}
    variant = {"labels": {}, "uncovered": {"u1"}, "baseline_useful": set(), "covered": set()}
    gold = {"primary": variant, "sens_rel": variant, "sens_irr": variant}
    result = direction_stability(oof_returned, gold)
    assert result["direction_stable"] is True
    assert set(result["labels"].values()) == {"leak-heavy"}


# ======================================================================
# baseline_macro_fdr(contamination floor 推导公式的输入,同一套计分规则)
# ======================================================================

def test_baseline_macro_fdr_uses_frozen_scoring_rules():
    top5_by_qid = {
        "q1": ["rel_1", "ex_1", "irr_1", "irr_2", "rel_2"],  # 3/5 irrelevant(含剔除卡最坏向)
        "q2": [],                                              # 空放行 → FDR=0
    }
    gold_variant = {
        "labels": {
            ("q1", "rel_1"): {"relevant": True, "useful": True},
            ("q1", "rel_2"): {"relevant": True, "useful": False},
            # irr_1/irr_2 标为 irrelevant;ex_1 是剔除卡(labels 缺席 = 最坏向计 irrelevant)
            ("q1", "irr_1"): {"relevant": False, "useful": False},
            ("q1", "irr_2"): {"relevant": False, "useful": False},
        },
        "covered": {"q1", "q2"}, "uncovered": set(), "baseline_useful": set(),
        "excluded": frozenset({("q1", "ex_1")}), "groups": {},
    }
    # macro = (0.6 + 0.0) / 2 = 0.3
    assert baseline_macro_fdr(top5_by_qid, gold_variant) == pytest.approx(0.3)


def test_baseline_macro_fdr_all_excluded_returned_counts_as_one():
    top5_by_qid = {"q1": ["ex_1", "ex_2"]}
    gold_variant = {
        "labels": {}, "covered": {"q1"}, "uncovered": set(), "baseline_useful": set(),
        "excluded": frozenset({("q1", "ex_1"), ("q1", "ex_2")}), "groups": {},
    }
    assert baseline_macro_fdr(top5_by_qid, gold_variant) == 1.0


# ======================================================================
# Layer 2:只吃 OOF + 字典序 + 简单度序平局
# ======================================================================

def test_layer2_select_rejects_non_oof_performance_source():
    with pytest.raises(ValueError, match="oof"):
        layer2_select(
            [{"arm_name": "cos_unified", "oof_summary": {
                "uncovered_allowed": 0, "b_useful_lost": 0, "covered_wrong_total": 0,
            }}],
            performance_source="resub",
        )


def test_layer2_select_lexicographic_order_and_simplicity_tiebreak():
    survivors = [
        {"arm_name": "ce_fixed", "oof_summary": {
            "uncovered_allowed": 1, "b_useful_lost": 0, "covered_wrong_total": 0}},
        {"arm_name": "cos_unified", "oof_summary": {
            "uncovered_allowed": 0, "b_useful_lost": 1, "covered_wrong_total": 0}},
        {"arm_name": "null_ref", "oof_summary": {
            "uncovered_allowed": 0, "b_useful_lost": 0, "covered_wrong_total": 2}},
        {"arm_name": "ce_znorm", "oof_summary": {
            "uncovered_allowed": 0, "b_useful_lost": 0, "covered_wrong_total": 0}},
        {"arm_name": "native_pertype", "oof_summary": {
            "uncovered_allowed": 0, "b_useful_lost": 0, "covered_wrong_total": 0}},
    ]
    result = layer2_select(survivors, performance_source="oof")
    order = [e["arm_name"] for e in result["pareto_table"]]
    # ①最小 uncovered_allowed 排最前:ce_znorm/native_pertype(0)先于 cos_unified(0 也并列,看②)/null_ref/ce_fixed(1)
    # ce_znorm 与 native_pertype 在 ①②③ 全 0 时打平,按④简单度序:native_pertype 更简单排前
    assert order[0] == "native_pertype"
    assert order[1] == "ce_znorm"
    assert order[-1] == "ce_fixed"  # uncovered_allowed=1,最差
    assert result["screen_leader"] == "native_pertype"


def test_layer2_select_empty_survivors_returns_no_leader():
    result = layer2_select([], performance_source="oof")
    assert result == {"screen_leader": None, "pareto_table": []}


def test_summarize_oof_for_layer2_counts_and_diagnostic():
    oof_returned = {
        "u1": [_card("x1", "agent_case", 0)],          # uncovered 放行(leak)
        "u2": [],                                        # uncovered 正确 abstain
        "b1": [_card("useful_1", "agent_case", 0)],       # B 命中
        "b2": [_card("not_useful_1", "agent_case", 0)],   # B 未命中(kill)
        "c1": [_card("irrelevant_1", "agent_case", 0), _card("relevant_not_useful_1", "agent_case", 0)],
    }
    gold_variant = {
        "labels": {
            ("b1", "useful_1"): {"relevant": True, "useful": True},
            ("b2", "not_useful_1"): {"relevant": True, "useful": False},
            ("c1", "irrelevant_1"): {"relevant": False, "useful": False},
            ("c1", "relevant_not_useful_1"): {"relevant": True, "useful": False},
        },
        "uncovered": {"u1", "u2"},
        "baseline_useful": {"b1", "b2"},
        "covered": {"c1"},
    }
    summary = summarize_oof_for_layer2(oof_returned, gold_variant)
    assert summary["uncovered_allowed"] == 1
    assert summary["b_useful_lost"] == 1
    assert summary["covered_wrong_total"] == 1  # 只有 irrelevant_1 算错卡
    assert summary["relevant_not_useful_diag"] == 1  # relevant_not_useful_1 进诊断列,不进错卡总数
    assert summary["error_direction"] == "balanced"  # leak(①=1) == kill(②=1)
