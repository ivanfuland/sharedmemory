"""§5/§6/§11 收口：把 stats 的数字 + 忠实检测门 + 成本 + 覆盖情况揉成一份 go/no-go 结论
与 markdown 报告(§11「报告落 reports/<当日>-everos-m1b-probe-result.md」)。

忠实是检测门,不是精率门(§5「~10-15 张只能探到毛病、不能认证 90%」)：
  0 张编造 -> provisional-clean；≥1 张编造 -> red-flag，无论抽样量多小。
样本未跑完(cap-stop)或覆盖不足(§4/§6 的空 wᵢ>0 格)一律强制 HOLD，不出 clean 结论(§11)。
"""
from __future__ import annotations

import collections
from dataclasses import dataclass

from everos_probe import stats

PASS_RATE_THRESHOLD = 0.15
COST_PER_CARD_THRESHOLD = 0.05


@dataclass(frozen=True)
class FaithfulnessAudit:
    cards_reviewed: int
    fabricated_count: int

    @property
    def red_flag(self) -> bool:
        return self.fabricated_count >= 1


@dataclass(frozen=True)
class CostSummary:
    total_spend_usd: float
    cards_generated: int

    @property
    def cost_per_card(self):
        if self.cards_generated <= 0:
            return None
        return self.total_spend_usd / self.cards_generated


def funnel_breakdown(outcomes: list) -> dict:
    """按 status 计数（含 FED_STATUSES 四态与 UNOBSERVED_STATUSES 三态），报告用漏斗归因。"""
    return dict(collections.Counter(o["status"] for o in outcomes))


def decide(ci_lower, ci_upper, faithfulness, cost, sample_incomplete, coverage_gap_strata) -> dict:
    """§6/§11 决策规则，唯一实现。

    None-safety 顺序契约（与 stats.compute_overall 的文档化不变式对齐）：先查
    sample_incomplete / coverage_gap_strata 强制 HOLD，再碰 ci_lower/ci_upper 做阈值
    比较——因为 compute_overall 在全部有权重的层都零观测时，ci_lower/ci_upper 为
    None 且 coverage_gap_strata 必非空；HOLD 分支提前 return 保证不会在 None 上做
    数值比较（否则 `None >= 0.15` TypeError）。不得调换这两段的先后顺序。
    """
    if sample_incomplete or coverage_gap_strata:
        reason = "sample-incomplete (cap-stop)" if sample_incomplete else "coverage-insufficient"
        return {"verdict": "HOLD", "reason": reason, "coverage_gap_strata": list(coverage_gap_strata)}

    cost_ok = cost.cost_per_card is not None and cost.cost_per_card <= COST_PER_CARD_THRESHOLD

    if ci_lower >= PASS_RATE_THRESHOLD and not faithfulness.red_flag and cost_ok:
        return {
            "verdict": "clear GO",
            "reason": (
                f"CI lower {ci_lower:.4f} >= {PASS_RATE_THRESHOLD}; "
                f"faithfulness clean ({faithfulness.cards_reviewed} reviewed, 0 fabricated); "
                f"cost/card {cost.cost_per_card:.4f} <= {COST_PER_CARD_THRESHOLD}"
            ),
        }

    if ci_upper < PASS_RATE_THRESHOLD or faithfulness.red_flag or not cost_ok:
        reasons = []
        if ci_upper < PASS_RATE_THRESHOLD:
            reasons.append(f"CI upper {ci_upper:.4f} < {PASS_RATE_THRESHOLD}")
        if faithfulness.red_flag:
            reasons.append(
                f"faithfulness red-flag ({faithfulness.fabricated_count} fabricated / "
                f"{faithfulness.cards_reviewed} reviewed)"
            )
        if not cost_ok:
            cpc = cost.cost_per_card
            reasons.append("no cards generated" if cpc is None else f"cost/card {cpc:.4f} > {COST_PER_CARD_THRESHOLD}")
        return {"verdict": "clear NO-GO", "reason": "; ".join(reasons)}

    return {
        "verdict": "marginal",
        "reason": f"CI [{ci_lower:.4f}, {ci_upper:.4f}] straddles {PASS_RATE_THRESHOLD} — 交 Ivan 二次判",
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
    }


def assemble_report(outcomes, w_raw, faithfulness, cost, sample_incomplete, floor: int = 5) -> dict:
    """顶层编排：stats.compute_overall + funnel + decide，供 Phase B Task 8 直接调用。"""
    overall = stats.compute_overall(outcomes, w_raw, floor)
    verdict = decide(
        overall["ci_lower"], overall["ci_upper"], faithfulness, cost,
        sample_incomplete, overall["coverage_gap_strata"],
    )
    return {
        "overall": overall,
        "funnel": funnel_breakdown(outcomes),
        "faithfulness": {
            "cards_reviewed": faithfulness.cards_reviewed,
            "fabricated_count": faithfulness.fabricated_count,
            "red_flag": faithfulness.red_flag,
        },
        "cost": {
            "total_spend_usd": cost.total_spend_usd,
            "cards_generated": cost.cards_generated,
            "cost_per_card": cost.cost_per_card,
        },
        "sample_incomplete": sample_incomplete,
        "verdict": verdict,
    }


def render_markdown(report: dict, title: str = "EverOS M1b 生产价值探针结果") -> str:
    """渲染成 §11 落盘用的 markdown 报告正文。

    C1 None-safety：`overall.weighted_pass_rate`/`ci_lower`/`ci_upper` 在
    `ci_method=="undefined"` 分支（全部有权重的层都零观测，见
    stats.compute_overall 文档化不变式）时是 `None`。§11 要求 HOLD 结论仍要落盘
    报告，不是跳过——所以这三个字段必须像既有的 `cost_per_card` 一样做
    `... if ... is not None else "n/a"` 降级渲染，不能直接 `:.4f` 格式化 None
    （会 TypeError，报告落盘那一步硬崩）。
    """
    o = report["overall"]
    v = report["verdict"]
    pass_rate_txt = f"{o['weighted_pass_rate']:.4f}" if o["weighted_pass_rate"] is not None else "n/a"
    ci_txt = (
        f"[{o['ci_lower']:.4f}, {o['ci_upper']:.4f}]"
        if o["ci_lower"] is not None and o["ci_upper"] is not None
        else "n/a"
    )
    lines = [
        f"# {title}",
        "",
        f"**verdict: {v['verdict']}** — {v['reason']}",
        "",
        "## 过门率",
        "",
        f"- 加权总体过门率：{pass_rate_txt}",
        f"- 95% CI（{o['ci_method']}）：{ci_txt}",
        f"- 覆盖缺口层：{', '.join(o['coverage_gap_strata']) or '无'}",
        "",
        "## 分层明细",
        "",
        "| 层 | wᵢ(重分后) | n | k | Wilson CI |",
        "|---|---|---|---|---|",
    ]
    for s in sorted(o["n"]):
        ni, ki = o["n"][s], o["k"][s]
        wci = o["per_stratum_wilson"].get(s)
        wci_txt = f"[{wci[0]:.4f}, {wci[1]:.4f}]" if wci else "n/a（nᵢ=0）"
        lines.append(f"| {s} | {o['w_reweighted'][s]:.4f} | {ni} | {ki} | {wci_txt} |")
    lines += ["", "## 漏斗归因", ""]
    for status, count in sorted(report["funnel"].items()):
        lines.append(f"- {status}：{count}")
    lines += [
        "",
        "## 忠实（检测门）",
        "",
        f"- 抽样审计：{report['faithfulness']['cards_reviewed']} 张",
        f"- 编造数：{report['faithfulness']['fabricated_count']}",
        f"- 结论：{'红旗' if report['faithfulness']['red_flag'] else 'provisional-clean'}",
        "",
        "## 成本",
        "",
        f"- 总花费：${report['cost']['total_spend_usd']:.4f}",
        f"- 生成卡数：{report['cost']['cards_generated']}",
        "- 成本/卡：" + (
            f"${report['cost']['cost_per_card']:.4f}" if report['cost']['cost_per_card'] is not None else "n/a"
        ),
        "",
        "## 样本完整性",
        "",
        f"- 是否跑完：{'否（cap 提前停）' if report['sample_incomplete'] else '是'}",
        "",
    ]
    return "\n".join(lines)
