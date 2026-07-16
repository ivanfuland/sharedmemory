"""P4 §Task 5:六个确定性机制臂(逐卡放行的纯函数)。

`ARMS: dict[str, Arm]`,每个 `Arm.apply(scored_query, theta) -> set[canonical_card_id]`。
规则照 P4 表逐字实现(见 plan §P4「候选臂」表),`llm_reference` 不在本任务(Task 7)。

边界纪律:所有阈值比较一律 `>=`(恰等于 θ 放行)。`native_pertype` / `cos_pertype`
是二维臂,`theta` 必须是 `(theta_c, theta_s)` 二元组,按候选的 `mem_type` 分别取用
`agent_case` 用 `theta_c`、`agent_skill` 用 `theta_s`(不做单值容错——防止误把同一
阈值套两型)。`ce_znorm` 用当前查询候选池自身算 population 均值/标准差(σ=0 时
全拦,不除零)。`null_ref` 用 `ScoredQuery.decoy_ce_by_type[mem_type]` 取同型 decoy
ce 分的 max;该型缺 decoy 分时按候选池数据完整性契约原生 `KeyError`(fail-loud,
不静默放行/拦截)。

⚠ **`null_ref` 的 `theta` 语义是 δ 余量(margin),不是绝对阈值**——判据是
`candidate_ce >= max(同型 decoy_ce) + theta`,theta 可以是负数(容许候选分略低于
最强 decoy 分仍放行)。这与其余五臂(`native_pertype`/`cos_unified`/`cos_pertype`/
`ce_fixed`/`ce_znorm`)的 theta 语义完全不同——那五臂的 theta 都是对候选自身分数
(或其 z-score)的绝对下限。Task 6 判据引擎枚举 `null_ref` 的候选 θ 网格时必须对
每张候选卡先算出 `ce − max(同型 decoy ce)` 的差值分布再取断点(允许负值断点),
不能像其余五臂那样直接对原始分数取断点。

`ScoredQuery`/候选沿用项目既有惯例:候选是 dict(`{canonical_card_id, mem_type,
source_rank, native_score, cos, ce}`,同 `probe_candidates.load_candidates` 的
字段命名),不额外包一层候选 dataclass。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ScoredQuery:
    """一条查询的全部候选打分 + 分型 decoy ce 参照(P4 null_ref 臂用)。

    candidates:每条 `{canonical_card_id, mem_type, source_rank, native_score, cos, ce}`。
    decoy_ce_by_type:`{"agent_case": [...], "agent_skill": [...]}`,每型的冻结 decoy
    段 ce 分列表(供 null_ref 取同型 max)。
    """
    query_id: str
    candidates: tuple[dict, ...]
    decoy_ce_by_type: dict[str, tuple[float, ...]]


def _arm_native_pertype(sq: ScoredQuery, theta) -> set[str]:
    theta_c, theta_s = theta
    return {
        c["canonical_card_id"] for c in sq.candidates
        if c["native_score"] >= (theta_c if c["mem_type"] == "agent_case" else theta_s)
    }


def _arm_cos_unified(sq: ScoredQuery, theta) -> set[str]:
    return {c["canonical_card_id"] for c in sq.candidates if c["cos"] >= theta}


def _arm_cos_pertype(sq: ScoredQuery, theta) -> set[str]:
    theta_c, theta_s = theta
    return {
        c["canonical_card_id"] for c in sq.candidates
        if c["cos"] >= (theta_c if c["mem_type"] == "agent_case" else theta_s)
    }


def _arm_ce_fixed(sq: ScoredQuery, theta) -> set[str]:
    return {c["canonical_card_id"] for c in sq.candidates if c["ce"] >= theta}


def _arm_ce_znorm(sq: ScoredQuery, theta) -> set[str]:
    ces = [c["ce"] for c in sq.candidates]
    n = len(ces)
    mu = sum(ces) / n if n else 0.0
    sigma = (sum((x - mu) ** 2 for x in ces) / n) ** 0.5 if n else 0.0
    if sigma == 0.0:
        return set()  # σ=0 全拦(P4 冻结规则,不除零)
    return {c["canonical_card_id"] for c in sq.candidates if (c["ce"] - mu) / sigma >= theta}


def _arm_null_ref(sq: ScoredQuery, theta) -> set[str]:
    allowed = set()
    for c in sq.candidates:
        decoys = sq.decoy_ce_by_type[c["mem_type"]]  # 缺同型 decoy 分原生 KeyError,不静默
        if c["ce"] >= max(decoys) + theta:
            allowed.add(c["canonical_card_id"])
    return allowed


@dataclass(frozen=True)
class Arm:
    name: str
    apply: Callable[[ScoredQuery, object], set[str]]


ARMS: dict[str, Arm] = {
    "native_pertype": Arm("native_pertype", _arm_native_pertype),
    "cos_unified": Arm("cos_unified", _arm_cos_unified),
    "cos_pertype": Arm("cos_pertype", _arm_cos_pertype),
    "ce_fixed": Arm("ce_fixed", _arm_ce_fixed),
    "ce_znorm": Arm("ce_znorm", _arm_ce_znorm),
    "null_ref": Arm("null_ref", _arm_null_ref),
}
