"""§5/§6 统计定义的唯一实现——STAT-CRITICAL，逐字编码，不得意译。

denominator 口径(§5)：分母 = 真正被喂到、给了过门机会的会话。
  structural_reject / semantic_reject / other（都被喂过，最终没过门）-> 入分母，分子 0。
  这四个状态字符串与 attribution.classify_session 的返回值逐字一致（"other" 不是 "other_reject"）。
  cap 截断未喂 / 坏样本剔除 / 不可恢复的喂失败 -> 未观测，不入分子也不入分母，单列覆盖缺口。
`passed` = 至少产出 1 张绑本 session 的卡（attribution.classify_session 的判据）。

CI 口径(§6)：分层加权方差，不是二项 Wilson——
  Var(p̂) = Σ wᵢ²·p̂ᵢ(1−p̂ᵢ)/nᵢ，95% CI = p̂ ± 1.96·√Var。
  空/零观测格(wᵢ>0 但 nᵢ=0) -> 权重按比例重分到有观测的格，CI 与加权率一律用重分后的 wᵢ。
  nᵢ 小(< floor)则退 bootstrap。
"""
from __future__ import annotations

import collections
import math
import random

# FED_STATUSES / UNOBSERVED_STATUSES 的字面量是与 Task 3 `attribution.classify_session`
# 之间的隐式契约：classify_session 的返回值必须逐字落在这两个集合之一。两边独立开发
# （Task2/3 并行），任一方改动字面量拼写都不会在类型层报错，只会在真跑时让样本静默
# 漏计或在 aggregate_fed_outcomes 里 fail-loud 崩溃。未来改动任一方，必须同步另一方。
FED_STATUSES = frozenset({"passed", "structural_reject", "semantic_reject", "other"})
UNOBSERVED_STATUSES = frozenset({"unobserved_cap", "unobserved_excluded", "unobserved_feed_failed"})

Z_95 = 1.96


def aggregate_fed_outcomes(outcomes: list) -> tuple:
    """只有 FED_STATUSES 计入分母/分子；UNOBSERVED_STATUSES 被跳过，不进 n/k(§5：cap 截断/
    坏样本剔除/不可恢复喂失败 = 未观测,不入分子也不入分母)。**任何既不在 FED_STATUSES 也
    不在 UNOBSERVED_STATUSES 里的 status 一律 fail-loud**——喂料层的一个拼写错误不该让样本
    悄悄从统计里消失（曾经是静默 skip，等同凭空丢数据不留痕迹）。"""
    n: dict = collections.defaultdict(int)
    k: dict = collections.defaultdict(int)
    for o in outcomes:
        status = o["status"]
        if status in FED_STATUSES:
            n[o["stratum"]] += 1
            if status == "passed":
                k[o["stratum"]] += 1
        elif status not in UNOBSERVED_STATUSES:
            raise ValueError(
                f"unrecognized status {status!r} for stratum {o.get('stratum')!r} — not in "
                f"FED_STATUSES or UNOBSERVED_STATUSES; a feed-layer typo must not vanish silently"
            )
    return dict(n), dict(k)


def reweight_for_zero_observed(w: dict, n: dict) -> dict:
    """spec §4/§5/§6：wᵢ>0 但 nᵢ=0 的格权重按比例重分到有观测的格(renormalize，不填 0、
    不跳过)。真空库格(wᵢ=0)天然保持 0，不算"重分"，也不触发覆盖缺口。
    全部格都无观测 -> raise（无法定义总体率，上层应转 HOLD，不该在此静默出数）。"""
    observed_w_sum = sum(w[s] for s in w if n.get(s, 0) > 0)
    if observed_w_sum <= 0:
        raise ValueError("no observed strata with n>0; cannot reweight (report layer should force HOLD)")
    return {s: (w[s] / observed_w_sum if n.get(s, 0) > 0 else 0.0) for s in w}


def coverage_gap_strata(w: dict, n: dict) -> list:
    """wᵢ>0 但 nᵢ=0 的格 = 真实覆盖缺口（区别于库中本就不存在的 wᵢ=0 格）。"""
    return sorted(s for s in w if w[s] > 0 and n.get(s, 0) == 0)


def weighted_pass_rate(w: dict, k: dict, n: dict) -> float:
    """Σ wᵢ·p̂ᵢ，只对 nᵢ>0 的格求和；调用方须传入 reweight 后的 w（未观测格已归零，
    天然不贡献）。"""
    total = 0.0
    for stratum, weight in w.items():
        ni = n.get(stratum, 0)
        if ni > 0:
            total += weight * (k.get(stratum, 0) / ni)
    return total


def stratified_variance(w: dict, k: dict, n: dict) -> float:
    """Var(p̂) = Σ wᵢ²·p̂ᵢ(1−p̂ᵢ)/nᵢ（§6，只对 nᵢ>0 的格求和，w 须已重分）。"""
    var = 0.0
    for stratum, weight in w.items():
        ni = n.get(stratum, 0)
        if ni > 0:
            pi = k.get(stratum, 0) / ni
            var += (weight ** 2) * pi * (1 - pi) / ni
    return var


def weighted_ci(w: dict, k: dict, n: dict, z: float = Z_95) -> tuple:
    """返回 (p̂, lower, upper)。裁到 [0,1]（比例不能越界；spec 公式本身未裁剪，裁剪是
    对退化情形——如 p̂ 接近 0/1 时区间跨界——的合理防御）。"""
    p_hat = weighted_pass_rate(w, k, n)
    var = stratified_variance(w, k, n)
    half = z * math.sqrt(var)
    return p_hat, max(0.0, p_hat - half), min(1.0, p_hat + half)


def wilson_ci(k: int, n: int, z: float = Z_95) -> tuple:
    """诊断用 per-stratum Wilson score interval（§6：「层内比例各报 Wilson 作诊断」）。
    n=0 时 raise（诊断层不该对空层调用；调用方应先按 n>0 过滤）。"""
    if n <= 0:
        raise ValueError("wilson_ci requires n > 0")
    p = k / n
    denom = 1 + (z * z) / n
    center = p + (z * z) / (2 * n)
    adj = z * math.sqrt(p * (1 - p) / n + (z * z) / (4 * n * n))
    return ((center - adj) / denom, (center + adj) / denom)


def should_bootstrap(n: dict, floor: int = 5) -> bool:
    """§6「nᵢ 小则退 bootstrap」：任一有观测(n>0)的格 n < floor 触发。floor 默认对齐
    §4 抽样的 per-stratum floor=5——同一个"小样本"阈值，不另造第二套数字。"""
    return any(0 < v < floor for v in n.values())


def _sessions_by_stratum(outcomes: list) -> dict:
    out: dict = collections.defaultdict(list)
    for o in outcomes:
        if o["status"] in FED_STATUSES:
            out[o["stratum"]].append(1 if o["status"] == "passed" else 0)
    return dict(out)


def bootstrap_ci(sessions_by_stratum: dict, w: dict, n_boot: int = 2000, seed: int = 20260713) -> tuple:
    """§6 bootstrap 退化路径：对每层的 fed 会话 0/1 结果做放回重抽样，逐次重算加权率，
    取经验分布 2.5/97.5 百分位作 95% CI。固定 seed 保证同一份 outcomes 每次跑出同一个 CI
    (§11「可复现」的延伸)。"""
    rng = random.Random(seed)
    boot_rates = []
    for _ in range(n_boot):
        rate = 0.0
        for stratum, weight in w.items():
            obs = sessions_by_stratum.get(stratum, [])
            if not obs:
                continue
            resample = [rng.choice(obs) for _ in obs]
            rate += weight * (sum(resample) / len(resample))
        boot_rates.append(rate)
    boot_rates.sort()
    lo_idx = int(0.025 * n_boot)
    hi_idx = min(int(0.975 * n_boot), n_boot - 1)
    return boot_rates[lo_idx], boot_rates[hi_idx]


def compute_overall(outcomes: list, w_raw: dict, floor: int = 5) -> dict:
    """顶层编排,供 report.py 调用。outcomes: [{"stratum":str,"status":str}, ...]。
    w_raw: §4 抽样阶段记录的真实库分层占比(未重分)。

    None-safety 不变式（Task 4 report.decide() 依赖此契约）：
    返回 dict 的 `weighted_pass_rate` / `ci_lower` / `ci_upper` 三者为 `None`
    **当且仅当** `w_raw` 中**所有权重 > 0 的层**都被 `coverage_gap_strata` 覆盖
    （即所有真正贡献权重的层都零观测，reweight_for_zero_observed 判定"总体率未
    定义"）。注意 `coverage_gap_strata` 本身只收 `wᵢ>0 且 nᵢ=0` 的层——库中真实
    权重为 0 的层（该层在真实 CASS 里本就不存在）永远不会出现在 coverage_gap_strata
    里，也不影响此不变式（它们不贡献权重，无需被"覆盖"）。此时 `ci_method`
    固定为 `"undefined"`。decide() 必须先检查 coverage_gap_strata（非空即
    §11 强制 HOLD）再碰 ci_lower/ci_upper，不能反过来先比较 CI 再查缺口——
    否则会在 None 上做数值比较，TypeError。任何后续改动都不得打破这条顺序契约。

    退化边缘（零正权重层）：本不变式假设 w_raw 至少有 1 个权重 > 0 的层。若 w_raw
    全部为 0（或为空），该假设 vacuously 满足于左边（ci=None 仍成立）但右边落空——
    coverage_gap_strata 只收 wᵢ>0 且 nᵢ=0 的层，零正权重层时天然为空列表，不会
    "覆盖"任何东西。这种输入在生产环境不现实，但为防止 decide() 在此退化边缘上对
    None 做数值比较而 cryptic TypeError，decide() 额外直接判 ci_lower/ci_upper
    是否为 None 并兜底 HOLD，不完全依赖本不变式。
    """
    n, k = aggregate_fed_outcomes(outcomes)
    unknown_strata = set(n) - set(w_raw)
    if unknown_strata:
        # 下面的 `{s: ... for s in w_raw}` 只按 w_raw 的键取数——若哪条 outcome 的 stratum
        # 字符串跟 w_raw 键对不上，会被悄悄丢弃、n/k 悄悄漏计。fail-loud，跟 status 字符串
        # 同等纪律（不静默丢样本）。
        raise ValueError(
            f"stratum label(s) present in outcomes but not in w_raw: {sorted(unknown_strata)} "
            f"(w_raw keys: {sorted(w_raw)}) — fix the stratum string upstream, do not silently drop"
        )
    # w_raw[s]==0 格代表"库中本就不存在该层"，理论上不该收到任何 fed 观测（spec §4）。
    # 若真的收到了(n>0)，多半是 sampling↔classification 之间 stratum 字符串标签漂移
    # （两层各自独立判定"这条属于哪层"，标签不一致）。跟 unknown_strata 同等纪律：
    # 不静默把这些样本按权重 0 丢掉、悄悄从 rate 里消失，fail-loud 逼上游查漂移。
    zero_weight_but_observed = sorted(s for s in n if w_raw.get(s, 0) == 0 and n[s] > 0)
    if zero_weight_but_observed:
        raise ValueError(
            f"stratum label(s) {zero_weight_but_observed} have w_raw==0 (library says this "
            f"stratum does not exist) but received fed observations (n>0) — this indicates a "
            f"sampling/classification stratum-label drift upstream; fix the stratum string, "
            f"do not silently drop these observations by letting them collapse to weight 0"
        )
    n_full = {s: n.get(s, 0) for s in w_raw}
    k_full = {s: k.get(s, 0) for s in w_raw}
    gap = coverage_gap_strata(w_raw, n_full)
    try:
        w_reweighted = reweight_for_zero_observed(w_raw, n_full)
    except ValueError:
        # 全部有权重的层都零观测(如 cap 在第一个会话前就停、或全部坏样本被剔除)：
        # reweight_for_zero_observed 按其契约 raise（无法定义总体率）。这里不吞异常瞎猜数，
        # 而是把"未定义"结构化透传给上层——report.decide() 见非空 coverage_gap_strata 会
        # 先于任何 CI 判断强制 HOLD(§11)，压根不会摸到这里的 None。
        return {
            "weighted_pass_rate": None,
            "ci_lower": None,
            "ci_upper": None,
            "ci_method": "undefined",
            "w_reweighted": dict(w_raw),
            "n": n_full,
            "k": k_full,
            "coverage_gap_strata": gap,
            "per_stratum_wilson": {},
        }
    p_hat, lo, hi = weighted_ci(w_reweighted, k_full, n_full)
    method = "analytic"
    if should_bootstrap(n_full, floor):
        blo, bhi = bootstrap_ci(_sessions_by_stratum(outcomes), w_reweighted)
        lo, hi, method = blo, bhi, "bootstrap"
    return {
        "weighted_pass_rate": p_hat,
        "ci_lower": lo,
        "ci_upper": hi,
        "ci_method": method,
        "w_reweighted": w_reweighted,
        "n": n_full,
        "k": k_full,
        "coverage_gap_strata": gap,
        "per_stratum_wilson": {s: wilson_ci(k_full[s], n_full[s]) for s in n_full if n_full[s] > 0},
    }
