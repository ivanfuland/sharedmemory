"""预注册指标(spec R4 §4/§10)。公式即 spec,改公式 = 改 spec。"""
from __future__ import annotations
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class QueryOutcome:
    query_id: str
    l1_relevant_ids: frozenset
    l1_useful_ids: frozenset
    top5_ids: tuple
    top5_relevant_ids: frozenset
    top5_useful_ids: frozenset


def wilson_lower(successes: int, n: int, z: float = 1.6449) -> float:
    if n == 0:
        return 0.0
    p = successes / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (center - margin) / denom


def band_verdict(p: float, n: int, lo: float) -> str:
    if n < 10:
        return "invalid_n"
    if p >= 0.5:
        v = "strong_pass"
    elif p >= 0.4:
        v = "borderline_pass"
    elif p >= 0.3:
        return "weak_signal"
    else:
        return "clear_fail"
    return v + ("_exploratory" if lo < 0.25 else "")


def compute_metrics(outcomes: list[QueryOutcome]) -> dict:
    n_total = len(outcomes)
    covered = [o for o in outcomes if o.l1_relevant_ids]
    uncovered = [o for o in outcomes if not o.l1_relevant_ids]
    n_cov = len(covered)
    useful_exists = [o for o in covered if o.l1_useful_ids]
    cov_useful_hits = [o for o in covered if o.top5_useful_ids]
    hits = [o for o in covered if o.l1_relevant_ids & set(o.top5_ids)]

    def hit_at(k: int) -> float:
        if not covered:
            return 0.0
        return sum(1 for o in covered if o.l1_relevant_ids & set(o.top5_ids[:k])) / n_cov

    def mrr() -> float:
        if not covered:
            return 0.0
        total = 0.0
        for o in covered:
            for rank, cid in enumerate(o.top5_ids, 1):
                if cid in o.l1_relevant_ids:
                    total += 1 / rank
                    break
        return total / n_cov

    def p_at_5() -> float:
        if not outcomes:
            return 0.0
        return sum(len(o.top5_relevant_ids) / max(len(o.top5_ids), 1) for o in outcomes) / n_total

    cov_hit = len(cov_useful_hits)
    return {
        "n_total": n_total,
        "n_covered": n_cov,
        "coverage": n_cov / n_total if n_total else 0.0,
        "useful_coverage": len(useful_exists) / n_total if n_total else 0.0,
        "covered_useful_hit_at_5": cov_hit / n_cov if n_cov else 0.0,
        "covered_useful_hit_wilson_lo": wilson_lower(cov_hit, n_cov),
        "global_useful_hit_at_5": sum(1 for o in outcomes if o.top5_useful_ids) / n_total if n_total else 0.0,
        "conditional_hit": len(hits) / n_cov if n_cov else 0.0,
        "conditional_useful": (sum(1 for o in hits if o.top5_useful_ids) / len(hits)) if hits else 0.0,
        "useful_exists_rate": len(useful_exists) / n_cov if n_cov else 0.0,
        "hit_at_1": hit_at(1), "hit_at_3": hit_at(3), "hit_at_5": hit_at(5),
        "mrr": mrr(),
        "precision_at_5": p_at_5(),
        "uncovered_pseudo_relevant_rate": _u_rel(uncovered),
        "uncovered_irrelevant_rate": _u_irr(uncovered),
        "n_uncovered": len(uncovered),
        # spec §5 预注册:L1=no 查询 top-5 的「无关」平均占比 ≥60% 触发 guard(codex R1 抓出方向反)
        "go_with_guard": bool(uncovered) and _u_irr(uncovered) >= 0.6,
    }


def _u_rel(uncovered) -> float:
    if not uncovered:
        return 0.0
    return sum(len(o.top5_relevant_ids) / max(len(o.top5_ids), 1) for o in uncovered) / len(uncovered)


def _u_irr(uncovered) -> float:
    if not uncovered:
        return 0.0
    return sum((len(o.top5_ids) - len(o.top5_relevant_ids)) / max(len(o.top5_ids), 1)
               for o in uncovered) / len(uncovered)
