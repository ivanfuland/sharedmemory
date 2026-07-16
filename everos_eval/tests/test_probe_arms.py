"""probe_arms.py 的测试(P4 §Task 5:六个确定性机制臂,边界 = 恰等于 θ 放行)。

fixture:4 候选(2 agent_case + 2 agent_skill)+ 分型 decoy ce 参照,数值手造以便
精确命中每臂的边界条件(恰等于 θ / max_decoy+δ 放行、σ=0 全拦、二维臂分型独立
判定)。z-norm 的边界 theta 用与实现相同的公式在测试里现算,避免手写浮点字面量
和实现产生位级不一致的假失败。
"""
from __future__ import annotations

import pytest

from everos_eval.probe_arms import ARMS, ScoredQuery

CANDIDATES = (
    {"canonical_card_id": "ac_1", "mem_type": "agent_case", "source_rank": 0,
     "native_score": 5.0, "cos": 0.5, "ce": 2.0},
    {"canonical_card_id": "ac_2", "mem_type": "agent_case", "source_rank": 1,
     "native_score": 3.0, "cos": 0.3, "ce": 1.0},
    {"canonical_card_id": "sk_1", "mem_type": "agent_skill", "source_rank": 0,
     "native_score": 9.0, "cos": 0.8, "ce": 4.0},
    {"canonical_card_id": "sk_2", "mem_type": "agent_skill", "source_rank": 1,
     "native_score": 7.0, "cos": 0.6, "ce": 3.0},
)

DECOY_CE_BY_TYPE = {
    "agent_case": (1.5, 1.0),
    "agent_skill": (3.5, 2.0),
}


@pytest.fixture
def sq() -> ScoredQuery:
    return ScoredQuery(query_id="q01", candidates=CANDIDATES, decoy_ce_by_type=DECOY_CE_BY_TYPE)


# ---- native_pertype:二维臂,恰等于 θ 放行,case/skill 各走各阈值 ----

def test_native_pertype_boundary_equal_passes_both_types(sq):
    # theta_c=5.0 恰等于 ac_1 native;theta_s=9.0 恰等于 sk_1 native
    got = ARMS["native_pertype"].apply(sq, (5.0, 9.0))
    assert got == {"ac_1", "sk_1"}


def test_native_pertype_case_and_skill_thresholds_independent(sq):
    # theta_c 很低放行两张 case,theta_s 很高全拦 skill
    got = ARMS["native_pertype"].apply(sq, (3.0, 100.0))
    assert got == {"ac_1", "ac_2"}


# ---- cos_unified:统一阈值,恰等于 θ 放行 ----

def test_cos_unified_boundary_equal_passes(sq):
    got = ARMS["cos_unified"].apply(sq, 0.5)
    assert got == {"ac_1", "sk_1", "sk_2"}


# ---- cos_pertype:二维臂,case/skill 各走各阈值,含恰等于边界 ----

def test_cos_pertype_boundary_equal_passes_both_types(sq):
    # theta_c=0.5 恰等于 ac_1 cos;theta_s=0.6 恰等于 sk_2 cos(边界放行)
    got = ARMS["cos_pertype"].apply(sq, (0.5, 0.6))
    assert got == {"ac_1", "sk_1", "sk_2"}


# ---- ce_fixed:统一阈值,恰等于 θ 放行 ----

def test_ce_fixed_boundary_equal_passes(sq):
    got = ARMS["ce_fixed"].apply(sq, 2.0)
    assert got == {"ac_1", "sk_1", "sk_2"}


# ---- ce_znorm:per-query z-score,σ=0 全拦,边界恰等于 z 放行 ----

def test_ce_znorm_boundary_equal_passes(sq):
    ces = [c["ce"] for c in CANDIDATES]
    n = len(ces)
    mu = sum(ces) / n
    sigma = (sum((x - mu) ** 2 for x in ces) / n) ** 0.5
    # 取 sk_2(ce=3.0)的精确 z 值作为 theta——与实现同公式现算,保证位级可复现的边界。
    theta = (3.0 - mu) / sigma
    got = ARMS["ce_znorm"].apply(sq, theta)
    assert got == {"sk_1", "sk_2"}  # sk_1 z 更高必过;sk_2 恰等于 theta 也应放行


def test_ce_znorm_sigma_zero_blocks_all():
    flat_candidates = tuple(dict(c, ce=2.0) for c in CANDIDATES)  # 全部 ce 相同 -> sigma=0
    sq_flat = ScoredQuery(query_id="q02", candidates=flat_candidates,
                           decoy_ce_by_type=DECOY_CE_BY_TYPE)
    got = ARMS["ce_znorm"].apply(sq_flat, theta=-100.0)  # 即便 theta 极低也应全拦
    assert got == set()


# ---- null_ref:ce(卡) >= max(同型 decoy ce) + delta,恰等于放行,分型独立 ----

def test_null_ref_boundary_equal_passes_both_types(sq):
    # agent_case max decoy ce=1.5,ac_1 ce=2.0 -> 2.0 == 1.5+0.5 恰等于边界
    # agent_skill max decoy ce=3.5,sk_1 ce=4.0 -> 4.0 == 3.5+0.5 恰等于边界
    got = ARMS["null_ref"].apply(sq, delta := 0.5)
    assert got == {"ac_1", "sk_1"}


def test_null_ref_missing_decoy_type_raises_keyerror():
    sq_partial = ScoredQuery(query_id="q03", candidates=CANDIDATES,
                              decoy_ce_by_type={"agent_case": (1.5, 1.0)})  # 缺 agent_skill
    with pytest.raises(KeyError):
        ARMS["null_ref"].apply(sq_partial, 0.5)


# ---- ARMS 注册表完整性(六臂,llm_reference 不在本任务) ----

def test_arms_registry_has_exactly_six_deterministic_arms():
    assert set(ARMS) == {
        "native_pertype", "cos_unified", "cos_pertype",
        "ce_fixed", "ce_znorm", "null_ref",
    }
    assert "llm_reference" not in ARMS  # Task 7,不在本任务范围
