"""P5 §Task 6:判据引擎——三 floor + 无量纲 slack maximin + group-LOO/最终拟合分离
+ Layer 2 字典序 + 误差方向标签。

**返回集唯一操作化(冻结,spec 重写照抄)**:`compute_returned` 就是这条唯一公式的
代码化——guard 只过滤不重排,对**全部**候选生成完整交错序(k 从实际候选数派生,
绝不写死 33/40),再稳定过滤,再取前 `limit`。Layer 1 与 Layer 2 全部指标只读
`compute_returned`/`compute_returned_for_query` 的输出,不允许绕过这层直接读
`ScoredQuery.candidates`。

**三 floor(query 级,分母随 gold 变体重推,不硬编码)**:
- uncovered abstain ≥ 0.80(abstain ⟺ 放行集为空;剔除卡的原始分数照常参与
  arm 判定,若剔除卡本身被放行,abstain 自然被破坏——不需要额外特判);
- covered useful(B 集,§P5 定义:未过滤 top5 含 ≥1 gold-useful 的 covered 查询)
  保留 ≥ 0.85(剔除卡不在 primary labels 里,`.get()` 落空即不算 useful);
- covered contamination 双门:①宏平均 FDR(covered 查询,abstain 记 0)≤ floor;
  ②非空放行条件 FDR(只在非空放行的 covered 查询上取平均)≤ 同一 floor。FDR
  计分:剔除卡在 `labels` 里缺席,primary 下 `.get(...).get("relevant") is not True`
  自然判定为 irrelevant(最坏向,分子分母都进);returned 非空但全为剔除卡强制
  FDR=1(保守,跨全部 gold 变体统一,不因 sens_rel 的乐观翻转而豁免)。

**阈值搜索(`fit_threshold`)**:候选 θ = 训练组内该信号相邻去重实测分值的区间
中点(不落在实测分值本身上)+ 上下 sentinel;对每个候选精确重算训练三门(不做
区间/单调假设)。选择规则:可行集内最大化 `min(s1, s2, s3, s3b)`,四个 slack 全部
无量纲、方向统一"越大越好":`s1 = abstain_rate − 0.80`、`s2 = useful_rate − 0.85`、
`s3 = floor − macro_FDR`、`s3b = floor − conditional_FDR`;并列取 θ 更大(二维按
`(theta_c, theta_s)` 字典序取大)。选出后返回前重断言训练全门;可行集空 → 该组
预测 abstain-all(`fit_threshold` 返回 `None`)。

**group-LOO 与最终拟合分离**:`grouped_loocv` 按父会话分组留出(同源查询同进
同出,杜绝泄漏),留出组的预测汇总算三 floor 供 `cv_pass` 判定;`final_fit` 同
程序在全部查询上拟合唯一 θ*,报告其 resubstitution 三门。幸存 ⟺ `cv_pass` 且
`final_fit` 可行。`layer2_select` 与幸存判定只用 OOF 预测——`performance_source`
非 `"oof"` 直接报错,不存在"喂 resub 也能跑"的旁路。

**Layer 2 排序(字典序,只用 OOF)**:①uncovered 放行查询数 → ②B 中丢
useful_hit 查询数 → ③covered 放行错卡总数 → ④简单度序。附带 returned 中
relevant-but-not-useful 卡计数(诊断,不参与排序)。**误差方向标签**由 OOF 计数
直接算:`kill = ②`、`leak = ①`,`kill>leak → "kill-heavy"`,`kill<leak →
"leak-heavy"`,相等 → `"balanced"`——禁止人工定性。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, Iterable, Sequence

ABSTAIN_FLOOR = 0.80
USEFUL_FLOOR = 0.85

# Layer 2 ④简单度序(字典序并列判据的最后一项)
SIMPLICITY_ORDER: tuple[str, ...] = (
    "native_pertype", "cos_unified", "cos_pertype", "null_ref", "ce_fixed", "ce_znorm",
)


# ======================================================================
# 返回集唯一操作化(冻结)
# ======================================================================

def merge_interleave(cases: Sequence[dict], skills: Sequence[dict], k: int) -> list[dict]:
    """确定性交错(与 everos_eval.retrieve.merge_top5 同一 skill-first 交错序,
    忠实重现、泛化到任意 k):skill/case 各按传入序列的自身顺序,skill 先,一侧
    耗尽另一侧补齐,直到凑够 k 个或双侧耗尽。k 从调用方传入,不在本函数内写死
    ——`compute_returned` 恒传 `k=len(cases)+len(skills)`,保证生成完整序列,
    不在过滤前就截断丢失候选。"""
    out: list[dict] = []
    i = 0
    while len(out) < k and (i < len(skills) or i < len(cases)):
        if i < len(skills):
            out.append(skills[i])
        if len(out) < k and i < len(cases):
            out.append(cases[i])
        i += 1
    return out


def compute_returned(cases: Sequence[dict], skills: Sequence[dict],
                      allowed: "Callable[[dict], bool] | Iterable[str]",
                      limit: int = 5) -> list[dict]:
    """唯一操作化(冻结,spec 重写照抄):
    `returned = [x for x in merge_interleave(cases, skills, k=len(cases)+len(skills)) if allowed(x)][:limit]`

    `allowed` 接受两种形式:可调用谓词 `x -> bool`(逐字对应冻结公式),或
    canonical_card_id 的容器(set/frozenset 等,按 `x["canonical_card_id"] in allowed`
    判定)——后者是给 Layer 1/Layer 2(消费 `Arm.apply()` 产出的放行集合)的
    人体工学包装,两种调用形式在语义上完全等价,都是"稳定过滤,不重排"。
    """
    predicate: Callable[[dict], bool]
    if callable(allowed):
        predicate = allowed
    else:
        allowed_set = allowed if isinstance(allowed, (set, frozenset)) else set(allowed)
        predicate = lambda x: x["canonical_card_id"] in allowed_set  # noqa: E731

    k = len(cases) + len(skills)
    order = merge_interleave(cases, skills, k=k)
    return [x for x in order if predicate(x)][:limit]


def split_candidates_by_type(candidates: Sequence[dict]) -> tuple[list[dict], list[dict]]:
    """按 `source_rank` 升序拆分候选为 (cases, skills)——与原始检索序一致,供
    `compute_returned` 的交错逻辑消费(交错逻辑假设两侧序列已经是各自的检索序)。"""
    cases = sorted((c for c in candidates if c["mem_type"] == "agent_case"),
                   key=lambda c: c["source_rank"])
    skills = sorted((c for c in candidates if c["mem_type"] == "agent_skill"),
                     key=lambda c: c["source_rank"])
    return cases, skills


def compute_returned_for_query(sq, allowed, limit: int = 5) -> list[dict]:
    """便捷包装:从 `ScoredQuery.candidates` 拆分 cases/skills 后调用
    `compute_returned`。判据引擎的日常调用入口(单条查询)。"""
    cases, skills = split_candidates_by_type(sq.candidates)
    return compute_returned(cases, skills, allowed, limit=limit)


# ======================================================================
# Layer 1:三 floor 聚合
# ======================================================================

def _excluded_by_qid(excluded: Iterable[tuple[str, str]]) -> dict[str, set]:
    by_qid: dict[str, set] = defaultdict(set)
    for qid, cid in excluded:
        by_qid[qid].add(cid)
    return by_qid


def _query_fdr(returned: list[dict], qid: str, labels: dict, excluded_by_qid: dict) -> float | None:
    """单条 covered 查询的 FDR。空放行 → 0.0(保守 floor 定义,不是"无意义");
    非空但全为剔除卡 → 1.0(保守,跨 gold 变体统一,盖过 sens_rel 的乐观翻转);
    否则 = irrelevant 计数 / returned 总数,irrelevant ⟺ `labels` 中该 (qid,cid)
    的 relevant 字段不是 True(剔除卡在 primary labels 缺席,`.get()` 落空同样
    判 irrelevant——这就是"primary 把剔除卡计为 irrelevant,分子分母都进")。"""
    if not returned:
        return 0.0
    ex = excluded_by_qid.get(qid, set())
    if all(c["canonical_card_id"] in ex for c in returned):
        return 1.0
    irrelevant = sum(
        1 for c in returned
        if labels.get((qid, c["canonical_card_id"]), {}).get("relevant") is not True
    )
    return irrelevant / len(returned)


def _aggregate_floors(returned_by_qid: dict[str, list[dict]], gold_variant: dict) -> dict:
    """从已算好的 `{qid: returned}` 聚合三 floor。与"如何算出 returned"解耦
    (grouped_loocv 的 OOF 聚合和 compute_layer1_floors 的单次评估共用这一层)。"""
    labels = gold_variant["labels"]
    covered = gold_variant["covered"]
    uncovered = gold_variant["uncovered"]
    baseline_useful = gold_variant["baseline_useful"]
    excluded_by_qid = _excluded_by_qid(gold_variant["excluded"])

    per_query: dict[str, dict] = {}

    abstain_hits = 0
    for qid in uncovered:
        returned = returned_by_qid.get(qid, [])
        is_abstain = len(returned) == 0
        per_query.setdefault(qid, {})["returned"] = returned
        per_query[qid]["abstain"] = is_abstain
        if is_abstain:
            abstain_hits += 1

    useful_hits = 0
    for qid in baseline_useful:
        returned = returned_by_qid.get(qid, [])
        hit = any(
            labels.get((qid, c["canonical_card_id"]), {}).get("useful") is True
            for c in returned
        )
        per_query.setdefault(qid, {})["returned"] = returned
        per_query[qid]["useful_hit"] = hit
        if hit:
            useful_hits += 1

    fdr_all, fdr_nonempty = [], []
    for qid in covered:
        returned = returned_by_qid.get(qid, [])
        per_query.setdefault(qid, {})["returned"] = returned
        fdr = _query_fdr(returned, qid, labels, excluded_by_qid)
        per_query[qid]["fdr"] = fdr
        fdr_all.append(fdr)
        if returned:
            fdr_nonempty.append(fdr)

    abstain_rate = (abstain_hits / len(uncovered)) if uncovered else 1.0
    useful_rate = (useful_hits / len(baseline_useful)) if baseline_useful else 1.0
    macro_fdr = (sum(fdr_all) / len(fdr_all)) if fdr_all else 0.0
    conditional_fdr = (sum(fdr_nonempty) / len(fdr_nonempty)) if fdr_nonempty else 0.0

    return {
        "abstain_rate": abstain_rate,
        "useful_rate": useful_rate,
        "macro_fdr": macro_fdr,
        "conditional_fdr": conditional_fdr,
        "per_query": per_query,
    }


def _restrict_gold_variant(gold_variant: dict, qids: Iterable[str]) -> dict:
    """把 gold_variant 的三个 query 级分母(covered/uncovered/baseline_useful)裁到
    只含 `qids` 内的查询——训练/单折拟合只应该被"看得见"的那部分查询的分母
    约束,不能被训练集之外的查询(留出组、未参与本次 sq_by_qid 的查询)拖累
    (它们在 `oof_returned`/`returned_by_qid` 里天然缺席,若不裁分母,`.get(qid, [])`
    的默认值会把它们当"错的"计入,污染训练期间的可行性判定)。labels/excluded/
    groups 不裁(按 (qid, cid) 键查,多余键无副作用)。"""
    qid_set = set(qids)
    return {
        "labels": gold_variant["labels"],
        "covered": gold_variant["covered"] & qid_set,
        "uncovered": gold_variant["uncovered"] & qid_set,
        "baseline_useful": gold_variant["baseline_useful"] & qid_set,
        "excluded": gold_variant["excluded"],
        "groups": gold_variant.get("groups", {}),
    }


def compute_layer1_floors(sq_list, arm, theta, gold_variant: dict) -> dict:
    """单个 (arm, theta) 在给定 ScoredQuery 集合上的三 floor 聚合。`theta=None`
    ⟹ abstain-all(可行集空场景的兜底,不调用 `arm.apply`)。"""
    returned_by_qid: dict[str, list[dict]] = {}
    for sq in sq_list:
        if theta is None:
            allowed: set = set()
        else:
            allowed = arm.apply(sq, theta)
        returned_by_qid[sq.query_id] = compute_returned_for_query(sq, allowed)
    return _aggregate_floors(returned_by_qid, gold_variant)


def check_floors_pass(floors: dict, contamination_floor: float,
                       abstain_floor: float = ABSTAIN_FLOOR,
                       useful_floor: float = USEFUL_FLOOR) -> bool:
    return (
        floors["abstain_rate"] >= abstain_floor
        and floors["useful_rate"] >= useful_floor
        and floors["macro_fdr"] <= contamination_floor
        and floors["conditional_fdr"] <= contamination_floor
    )


def floor_slacks(floors: dict, contamination_floor: float,
                  abstain_floor: float = ABSTAIN_FLOOR,
                  useful_floor: float = USEFUL_FLOOR) -> tuple[float, float, float, float]:
    """四个无量纲 slack,方向统一"越大越好"。"""
    s1 = floors["abstain_rate"] - abstain_floor
    s2 = floors["useful_rate"] - useful_floor
    s3 = contamination_floor - floors["macro_fdr"]
    s3b = contamination_floor - floors["conditional_fdr"]
    return s1, s2, s3, s3b


# ======================================================================
# θ 网格枚举(相邻去重实测分值区间中点 + sentinel)
# ======================================================================

def enumerate_theta_candidates(values: Sequence[float]) -> list[float]:
    """1D:相邻去重值的区间中点(不落在实测分值本身上)+ 上下 sentinel。空输入
    退化为单个 0.0 sentinel(不可行只会导致 fit_threshold 返回 None,不崩)。"""
    uniq = sorted(set(values))
    if not uniq:
        return [0.0]
    if len(uniq) == 1:
        v = uniq[0]
        return [v - 1.0, v + 1.0]
    mids = [(uniq[i] + uniq[i + 1]) / 2 for i in range(len(uniq) - 1)]
    return [uniq[0] - 1.0] + mids + [uniq[-1] + 1.0]


def enumerate_theta_candidates_pertype(case_values: Sequence[float],
                                        skill_values: Sequence[float]) -> list[tuple[float, float]]:
    """2D(native_pertype/cos_pertype):两型各自独立取区间中点断点,再做笛卡尔积。"""
    case_grid = enumerate_theta_candidates(case_values)
    skill_grid = enumerate_theta_candidates(skill_values)
    return [(tc, ts) for tc in case_grid for ts in skill_grid]


def _ce_znorm_values(sq_list) -> list[float]:
    """ce_znorm 的断点取值:每条查询自身的 population z-score(与 `_arm_ce_znorm`
    同公式现算,避免位级不一致)。σ=0 的查询贡献不出有信息量的断点(该查询下
    该臂对任意 theta 全拦),跳过。"""
    vals: list[float] = []
    for sq in sq_list:
        ces = [c["ce"] for c in sq.candidates]
        n = len(ces)
        if n == 0:
            continue
        mu = sum(ces) / n
        sigma = (sum((x - mu) ** 2 for x in ces) / n) ** 0.5
        if sigma == 0.0:
            continue
        vals.extend((ce - mu) / sigma for ce in ces)
    return vals


def _null_ref_margin_values(sq_list) -> list[float]:
    """null_ref 的断点取值:每张候选的 `ce − max(同型 decoy ce)` 差值(δ 余量语义,
    详见 probe_arms.py 的 null_ref 警告)。"""
    vals: list[float] = []
    for sq in sq_list:
        for c in sq.candidates:
            decoys = sq.decoy_ce_by_type[c["mem_type"]]
            vals.append(c["ce"] - max(decoys))
    return vals


def arm_theta_grid(sq_list, arm_name: str) -> list:
    """按臂名派发到对应信号取值 + 断点枚举——`fit_threshold` 的调用方
    (`grouped_loocv`/`final_fit`)用它现算每折/每次拟合的候选 θ 网格。"""
    if arm_name == "native_pertype":
        case_vals = [c["native_score"] for sq in sq_list for c in sq.candidates
                     if c["mem_type"] == "agent_case"]
        skill_vals = [c["native_score"] for sq in sq_list for c in sq.candidates
                      if c["mem_type"] == "agent_skill"]
        return enumerate_theta_candidates_pertype(case_vals, skill_vals)
    if arm_name == "cos_pertype":
        case_vals = [c["cos"] for sq in sq_list for c in sq.candidates
                     if c["mem_type"] == "agent_case"]
        skill_vals = [c["cos"] for sq in sq_list for c in sq.candidates
                      if c["mem_type"] == "agent_skill"]
        return enumerate_theta_candidates_pertype(case_vals, skill_vals)
    if arm_name == "cos_unified":
        vals = [c["cos"] for sq in sq_list for c in sq.candidates]
        return enumerate_theta_candidates(vals)
    if arm_name == "ce_fixed":
        vals = [c["ce"] for sq in sq_list for c in sq.candidates]
        return enumerate_theta_candidates(vals)
    if arm_name == "ce_znorm":
        return enumerate_theta_candidates(_ce_znorm_values(sq_list))
    if arm_name == "null_ref":
        return enumerate_theta_candidates(_null_ref_margin_values(sq_list))
    raise ValueError(f"arm_theta_grid: unknown arm_name {arm_name!r}")


# ======================================================================
# fit_threshold:maximin + 字典序平局 + 返回前重断言
# ======================================================================

def _theta_sort_key(theta) -> tuple:
    return theta if isinstance(theta, tuple) else (theta,)


def fit_threshold(theta_grid: Sequence, evaluate_fn: Callable[[object], dict],
                   contamination_floor: float,
                   abstain_floor: float = ABSTAIN_FLOOR,
                   useful_floor: float = USEFUL_FLOOR):
    """`theta_grid` 上逐点精确重算三门(`evaluate_fn(theta) -> floors dict`,不做
    区间/单调假设),可行集内 maximin 四个无量纲 slack,并列取 θ 更大(二维按
    `(theta_c, theta_s)` 字典序取大)。可行集空 → 返回 `None`(abstain-all)。
    选出后返回前重断言训练全门(精确重算,不复用挑选过程中的缓存值,防止选点
    与最终校验用了不同 evaluate_fn 状态)。"""
    best_theta = None
    best_slack = None
    best_key = None

    for theta in theta_grid:
        floors = evaluate_fn(theta)
        s1, s2, s3, s3b = floor_slacks(floors, contamination_floor, abstain_floor, useful_floor)
        slack = min(s1, s2, s3, s3b)
        if slack < 0:
            continue
        key = _theta_sort_key(theta)
        if (best_theta is None or slack > best_slack
                or (slack == best_slack and key > best_key)):
            best_theta, best_slack, best_key = theta, slack, key

    if best_theta is None:
        return None

    reassert_floors = evaluate_fn(best_theta)
    s1, s2, s3, s3b = floor_slacks(reassert_floors, contamination_floor, abstain_floor, useful_floor)
    if min(s1, s2, s3, s3b) < 0:
        # 显式 raise 而非裸 assert:`python -O` 会剥掉 assert 语句,这道返回前重断言
        # 是判据引擎的正确性门禁,不允许被解释器优化开关静默关掉。
        raise AssertionError(
            f"fit_threshold: 选出的 theta={best_theta!r} 重断言失败"
            f"(s1={s1!r} s2={s2!r} s3={s3!r} s3b={s3b!r})"
        )
    return best_theta


# ======================================================================
# grouped_loocv / final_fit / transport_check
# ======================================================================

def grouped_loocv(sq_by_qid: dict, arm, gold_variant: dict, contamination_floor: float) -> dict:
    """按父会话组留出(`gold_variant["groups"]`):每折训练集 = 全部查询减去该组,
    候选 θ 网格与拟合都只用训练集数据(留出组的分数不参与,杜绝泄漏);留出组
    用该折 θ*(或 `None` ⟹ abstain-all)产出预测,汇总成 OOF `returned_by_qid`,
    OOF 聚合三 floor 供 `cv_pass` 判定。同组查询天然同进同出——按 group 迭代,
    组内查询作为一个整体一起被留出,不存在组内拆分。"""
    groups = gold_variant["groups"]
    all_qids = set(sq_by_qid)

    oof_returned: dict[str, list[dict]] = {}
    fold_thetas: dict[str, object] = {}
    fold_train_qids: dict[str, list[str]] = {}

    for group_key, held_out_qids in groups.items():
        held_out = set(held_out_qids)
        train_qids = sorted(all_qids - held_out)
        fold_train_qids[group_key] = train_qids
        train_sq = [sq_by_qid[q] for q in train_qids]
        scoped_gold = _restrict_gold_variant(gold_variant, train_qids)

        grid = arm_theta_grid(train_sq, arm.name)

        def evaluate_fn(theta, _train_sq=train_sq, _scoped_gold=scoped_gold):
            return compute_layer1_floors(_train_sq, arm, theta, _scoped_gold)

        theta = fit_threshold(grid, evaluate_fn, contamination_floor)
        fold_thetas[group_key] = theta

        for qid in held_out:
            if qid not in sq_by_qid:
                continue
            sq = sq_by_qid[qid]
            allowed = set() if theta is None else arm.apply(sq, theta)
            oof_returned[qid] = compute_returned_for_query(sq, allowed)

    cv_floors = _aggregate_floors(oof_returned, gold_variant)
    cv_pass = check_floors_pass(cv_floors, contamination_floor)

    return {
        "oof_returned": oof_returned,
        "fold_thetas": fold_thetas,
        "fold_train_qids": fold_train_qids,
        "cv_floors": cv_floors,
        "cv_pass": cv_pass,
    }


def final_fit(sq_by_qid: dict, arm, gold_variant: dict, contamination_floor: float) -> dict:
    """全部查询上拟合唯一 θ*,报告 resubstitution 三门。**不参与幸存判定以外的
    任何排名**(P0-3:Layer 2 只吃 OOF)。"""
    qids = sorted(sq_by_qid)
    sq_list = [sq_by_qid[q] for q in qids]
    scoped_gold = _restrict_gold_variant(gold_variant, qids)
    grid = arm_theta_grid(sq_list, arm.name)

    def evaluate_fn(theta):
        return compute_layer1_floors(sq_list, arm, theta, scoped_gold)

    theta_star = fit_threshold(grid, evaluate_fn, contamination_floor)
    if theta_star is None:
        return {"theta_star": None, "resub_floors": None, "feasible": False}

    resub_floors = compute_layer1_floors(sq_list, arm, theta_star, scoped_gold)
    feasible = check_floors_pass(resub_floors, contamination_floor)
    return {"theta_star": theta_star, "resub_floors": resub_floors, "feasible": feasible}


def apply_fixed_fold_thetas(sq_by_qid: dict, arm, gold_variant: dict, fold_thetas: dict) -> dict:
    """把已经拟合好的每折 θ(通常来自另一轮/另一份 score matrix 的
    `grouped_loocv`)原样(不重新拟合)运输到给定的 `sq_by_qid` 上,按
    `gold_variant["groups"]` 的分组结构决定每条查询用哪一折的 θ。数值漂移门
    (5 轮打乱重打分)复用同一折分组、只换 score matrix 时用它——同一组织下的
    OOF `returned` 若在多轮打分间不一致,即判 `FAIL-fragile-score`。"""
    groups = gold_variant["groups"]
    returned_by_qid: dict[str, list[dict]] = {}
    for group_key, qids in groups.items():
        theta = fold_thetas.get(group_key)
        for qid in qids:
            if qid not in sq_by_qid:
                continue
            sq = sq_by_qid[qid]
            allowed = set() if theta is None else arm.apply(sq, theta)
            returned_by_qid[qid] = compute_returned_for_query(sq, allowed)
    return returned_by_qid


def survives(cv_result: dict, final_result: dict) -> bool:
    """幸存 ⟺ cv_performance 过三 floor 且 final fit 可行。"""
    return bool(cv_result["cv_pass"]) and bool(final_result["feasible"])


def transport_check(theta_star, arm, sq_by_qid: dict, gold_variant: dict,
                     contamination_floor: float) -> dict:
    """θ 运输门:θ* 原样(不重新拟合)运输到另一套 score matrix / gold 变体上,
    重算三 floor 并判定是否仍过门——不满足即运输失败,不代表整体幸存判定
    (幸存判定只看训练用的 gold 变体的 cv/final;transport_check 是敏感性诊断)。"""
    qids = sorted(sq_by_qid)
    sq_list = [sq_by_qid[q] for q in qids]
    scoped_gold = _restrict_gold_variant(gold_variant, qids)
    floors = compute_layer1_floors(sq_list, arm, theta_star, scoped_gold)
    passed = check_floors_pass(floors, contamination_floor)
    return {"floors": floors, "passed": passed}


# ======================================================================
# Layer 2:字典序 + 误差方向标签
# ======================================================================

def summarize_oof_for_layer2(oof_returned: dict[str, list[dict]], gold_variant: dict) -> dict:
    """从 OOF `returned_by_qid` 算 Layer 2 排序需要的四个计数 + 诊断列。"""
    labels = gold_variant["labels"]
    uncovered = gold_variant["uncovered"]
    baseline_useful = gold_variant["baseline_useful"]
    covered = gold_variant["covered"]

    uncovered_allowed = sum(
        1 for qid in uncovered
        if qid in oof_returned and len(oof_returned[qid]) > 0
    )

    b_useful_lost = 0
    for qid in baseline_useful:
        returned = oof_returned.get(qid, [])
        hit = any(labels.get((qid, c["canonical_card_id"]), {}).get("useful") is True
                   for c in returned)
        if not hit:
            b_useful_lost += 1

    covered_wrong_total = 0
    relevant_not_useful_diag = 0
    for qid in covered:
        for c in oof_returned.get(qid, []):
            lbl = labels.get((qid, c["canonical_card_id"]), {})
            if lbl.get("relevant") is not True:
                covered_wrong_total += 1
            elif lbl.get("useful") is not True:
                relevant_not_useful_diag += 1

    return {
        "uncovered_allowed": uncovered_allowed,
        "b_useful_lost": b_useful_lost,
        "covered_wrong_total": covered_wrong_total,
        "relevant_not_useful_diag": relevant_not_useful_diag,
        "error_direction": error_direction_label(kill=b_useful_lost, leak=uncovered_allowed),
    }


def error_direction_label(kill: int, leak: int) -> str:
    """`kill` = Layer2 ②(B 中丢 useful 查询数);`leak` = Layer2 ①(uncovered 放行
    查询数)。由 runner 从 OOF 计数直接算,禁止 report 作者人工定性。"""
    if kill > leak:
        return "kill-heavy"
    if kill < leak:
        return "leak-heavy"
    return "balanced"


def direction_stability(oof_returned: dict[str, list[dict]], gold_by_variant: dict,
                         variants: tuple[str, ...] = ("primary", "sens_rel", "sens_irr")) -> dict:
    """误差方向稳定性(P1-12):同一份 OOF 预测在三套 gold 变体下各算一次误差方向
    标签,稳定 ⟺ 三串完全一致。标签由 `summarize_oof_for_layer2` 的计数直接算,
    禁止人工定性。"""
    labels = {
        v: summarize_oof_for_layer2(oof_returned, gold_by_variant[v])["error_direction"]
        for v in variants
    }
    return {"labels": labels, "direction_stable": len(set(labels.values())) == 1}


def baseline_macro_fdr(returned_ids_by_qid: dict[str, list[str]], gold_variant: dict) -> float:
    """未过滤 top5 的基线宏 FDR(contamination floor 推导公式的输入):对
    `{qid: [top5 canonical_card_id, ...]}` 按本模块**同一套**正式计分规则
    (剔除卡最坏向计 irrelevant、returned 全剔除卡 FDR=1、空放行 FDR=0)算
    covered 查询上的宏平均 FDR——floor 公式的"基线"必须与 Layer 1 的 FDR 口径
    逐字一致,不允许另写一份计分逻辑产生口径漂移。"""
    returned_by_qid = {
        qid: [{"canonical_card_id": cid} for cid in ids]
        for qid, ids in returned_ids_by_qid.items()
    }
    return _aggregate_floors(returned_by_qid, gold_variant)["macro_fdr"]


def layer2_select(survivors: Sequence[dict], performance_source: str = "oof") -> dict:
    """字典序:①uncovered_allowed → ②b_useful_lost → ③covered_wrong_total →
    ④简单度序。`survivors` 每项须含 `arm_name` 与 `oof_summary`
    (`summarize_oof_for_layer2` 的产出)。`performance_source` 只接受 `"oof"`——
    喂 `"resub"` 或任何其它值直接报错,这是 P0-3 的接口级冻结,不是可选校验。
    交付物是幸存臂清单 + Pareto 表;字典序第一名只叫 `screen_leader`(非证据性
    默认推荐)。"""
    if performance_source != "oof":
        raise ValueError(
            f"layer2_select: performance_source 必须是 'oof',收到 {performance_source!r}"
            "——Layer 2 与幸存判定只用 OOF(留出)预测,final fit/resub 不参与任何排名(P0-3)"
        )
    if not survivors:
        return {"screen_leader": None, "pareto_table": []}

    def _key(entry: dict) -> tuple:
        m = entry["oof_summary"]
        simplicity_rank = SIMPLICITY_ORDER.index(entry["arm_name"])
        return (m["uncovered_allowed"], m["b_useful_lost"], m["covered_wrong_total"], simplicity_rank)

    ranked = sorted(survivors, key=_key)
    return {"screen_leader": ranked[0]["arm_name"], "pareto_table": ranked}
