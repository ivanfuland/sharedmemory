"""gold 补全 + 字段级仲裁(P3):L1 vs 统一第二判(sj)双源标签,冲突剔除 + 双向敏感性 + 父会话分组。

**保守计分(唯一权威定义)**:字段冲突(relevant 或 useful 任一不一致)的元组进剔除集,primary
gold 里既不算相关也不算无关(不出现在 primary 的 labels 里)。sens_rel/sens_irr 是双向敏感性:
只翻有分歧的字段(两 judge 一致的字段不动)——sens_rel 把分歧字段解到"相关/有用"(乐观),
sens_irr 解到"不相关/无用"(悲观)。三变体的 relevant_conflicts/useful_conflicts/excluded/groups
彼此相同(仲裁规则与父会话分组不依赖 primary/sens 的选择),只有 labels/covered/uncovered/
baseline_useful 随变体变化。

coverage estimand = 候选池内(P3):covered ⟺ 该查询候选(sj job 派生)中 ∃ gold-relevant。
候选池外的 (query, card) 对不参与本模块的任何指标(L1 全笛卡尔积只用于闭合校验,不进 labels)。
baseline_useful 是 **未过滤 top5 口径**(§P5 B 定义,与 covered 的候选池口径不同):
B = {covered 查询: retrieval.jsonl 该查询 synthetic 行的 top5(5 张卡)含 ≥1 张 gold-useful 卡},
剔除集卡在 primary 里不算 useful(不出现在 labels);sens 变体按各自翻转后的标签计。

不许人工手改标签;expected-ID 闭合失败(L1/second-judge 有缺行、top5 第三来源诊断对不上、
sj job 结构违规)一律 fail-loud(ValueError),不静默降级——这是数据完整性前置校验,不是可选项。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from everos_eval.judge_io import parse_verdicts
from everos_eval.retrieve import canonical_id

_SUBAGENT_SUFFIX = re.compile(r"/subagents/.*")


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _split_sj_job_id(job_id: str) -> tuple[str, str]:
    # "sj:{query_id}:{card_id}",card_id 是自由文本(卡标题)可能含半角冒号,maxsplit=2 防误切
    # (同 scripts/eval_run_m1c.py:_by_q 对 l1 job_id 的处理纪律)。
    _, qid, cid = job_id.split(":", 2)
    return qid, cid


def _parent_session_groups(queries: list[dict]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for q in queries:
        key = _SUBAGENT_SUFFIX.sub("", q["external_id"])
        groups.setdefault(key, []).append(q["query_id"])
    for qids in groups.values():
        qids.sort()
    return groups


def load_gold(data_dir: Path, second_judge_dir: Path,
              expected_candidates_per_query: int = 33) -> dict:
    """返回 {"primary": {...}, "sens_rel": {...}, "sens_irr": {...}},每个值含
    labels / relevant_conflicts / useful_conflicts / excluded(并集) / covered / uncovered /
    baseline_useful / groups(父会话分组)。expected-ID 缺行 / 第三来源诊断不符 / sj job 结构
    违规一律抛 ValueError(fail-loud,不静默丢数据)。
    expected_candidates_per_query:每查询候选数不变量(真实数据 33,mini fixture 可传小值),
    用于 jobs.jsonl 的独立完整性校验——sj expected 集不能拿 jobs.jsonl 自证自洽,jobs 和
    verdicts 两边自洽地缺同一行时必须靠这条结构不变量抓出来。"""
    data_dir = Path(data_dir)
    second_judge_dir = Path(second_judge_dir)

    queries = _read_jsonl(data_dir / "queryset.jsonl")
    cards = _read_jsonl(data_dir / "cards.jsonl")
    query_ids = [q["query_id"] for q in queries]

    # ---- L1 闭合:queryset × cards 派生的完整笛卡尔积(真实数据 30×54=1620) ----
    l1_expected = {f"l1:{qid}:{c['card_id']}" for qid in query_ids for c in cards}
    l1_verdicts, l1_bad = parse_verdicts(data_dir / "l1_verdicts.jsonl", "l1",
                                         expected_job_ids=l1_expected)
    if l1_bad:
        raise ValueError(f"L1 verdicts 不闭合(共 {len(l1_bad)} 条): {l1_bad[:10]}")

    # ---- top5 第三来源诊断闭合(仅作诊断,不进 labels):派生集须与冻结 top5_jobs.jsonl 精确相等 ----
    # 顺路收集每查询的未过滤 top5 卡 id(canonical 归一,规则同 load_candidates)——
    # baseline_useful 的 §P5 权威口径是这 5 张卡,不是 33 候选池。
    top5_expected = set()
    top5_by_qid: dict[str, list[str]] = {}
    for row in _read_jsonl(data_dir / "retrieval.jsonl"):
        if row.get("variant") != "synthetic":
            continue
        ids = []
        for rank, item in enumerate(row["top5"], 1):
            top5_expected.add(f"top5:{row['query_id']}:{rank}:{item['id']}")
            ids.append(canonical_id(item["id"], item["mem_type"]))
        top5_by_qid[row["query_id"]] = ids
    top5_actual = {j["job_id"] for j in _read_jsonl(data_dir / "top5_jobs.jsonl")}
    if top5_actual != top5_expected:
        missing = sorted(top5_expected - top5_actual)[:5]
        extra = sorted(top5_actual - top5_expected)[:5]
        raise ValueError(
            f"top5_jobs.jsonl 与派生 job-id 集不一致(第三来源诊断失败): missing={missing} extra={extra}"
        )

    # ---- 统一第二判(sj)job 结构守卫:无 rank 字段、无 top5: 前缀(R5 协议要求) ----
    sj_jobs = _read_jsonl(second_judge_dir / "jobs.jsonl")
    for job in sj_jobs:
        if "rank" in job:
            raise ValueError(f"sj job 违反 R5 协议(不应含 rank 字段): {job['job_id']}")
        if job["job_id"].startswith("top5:"):
            raise ValueError(f"sj job 违反 R5 协议(job_id 不应带 top5: 前缀): {job['job_id']}")

    # ---- sj jobs 独立完整性校验(不自证自洽):jobs.jsonl 自身损坏(如 jobs 与 verdicts
    # 两边自洽地缺同一行)时,靠"每查询恰 N 条 / 总数 = 查询数×N / job_id 唯一 /
    # query_id 集合与 queryset 精确相等"这组结构不变量 fail-loud ----
    sj_ids = [job["job_id"] for job in sj_jobs]
    if len(set(sj_ids)) != len(sj_ids):
        dupes = sorted(jid for jid, n in Counter(sj_ids).items() if n > 1)[:5]
        raise ValueError(f"sj jobs.jsonl 有重复 job_id: {dupes}")
    per_query = Counter(_split_sj_job_id(jid)[0] for jid in sj_ids)
    if set(per_query) != set(query_ids):
        missing_q = sorted(set(query_ids) - set(per_query))
        extra_q = sorted(set(per_query) - set(query_ids))
        raise ValueError(
            f"sj jobs.jsonl 的 query_id 集合与 queryset 不符: missing={missing_q} extra={extra_q}"
        )
    bad_counts = {q: n for q, n in per_query.items() if n != expected_candidates_per_query}
    if bad_counts:
        raise ValueError(
            f"sj jobs.jsonl 每查询候选数须恰为 {expected_candidates_per_query},违规: "
            f"{dict(sorted(bad_counts.items())[:5])}"
        )
    expected_total = len(query_ids) * expected_candidates_per_query
    if len(sj_jobs) != expected_total:  # 前两条不变量已蕴含,双保险显式断言
        raise ValueError(f"sj jobs.jsonl 总数 {len(sj_jobs)} != {expected_total}")

    # ---- sj 闭合:990 全量(候选池 = sj jobs 派生,不是 L1 的 1620 全笛卡尔积) ----
    sj_expected = set(sj_ids)
    sj_verdicts, sj_bad = parse_verdicts(second_judge_dir / "verdicts.jsonl", "sj",
                                         expected_job_ids=sj_expected)
    if sj_bad:
        raise ValueError(f"second-judge verdicts 不闭合(共 {len(sj_bad)} 条): {sj_bad[:10]}")

    # 每个 synthetic 候选恰有 L1 + sj 两份标签:逐候选核对 L1 侧存在对应记录
    candidates_by_query: dict[str, list[str]] = {}
    for job in sj_jobs:
        qid, cid = _split_sj_job_id(job["job_id"])
        candidates_by_query.setdefault(qid, []).append(cid)
        l1_key = f"l1:{qid}:{cid}"
        if l1_key not in l1_verdicts:
            raise ValueError(f"候选 {qid}/{cid} 缺 L1 标签(l1_verdicts 无 {l1_key})")

    # ---- 字段级冲突仲裁 ----
    relevant_conflicts: set[tuple[str, str]] = set()
    useful_conflicts: set[tuple[str, str]] = set()
    primary_labels: dict[tuple[str, str], dict] = {}
    rel_labels: dict[tuple[str, str], dict] = {}
    irr_labels: dict[tuple[str, str], dict] = {}

    for qid, cids in candidates_by_query.items():
        for cid in cids:
            key = (qid, cid)
            l1v = l1_verdicts[f"l1:{qid}:{cid}"]
            sjv = sj_verdicts[f"sj:{qid}:{cid}"]
            rel_conflict = l1v["relevant"] != sjv["relevant"]
            use_conflict = l1v["useful"] != sjv["useful"]
            if rel_conflict:
                relevant_conflicts.add(key)
            if use_conflict:
                useful_conflicts.add(key)
            if not (rel_conflict or use_conflict):
                primary_labels[key] = {"relevant": l1v["relevant"], "useful": l1v["useful"]}
            rel_labels[key] = {
                "relevant": True if rel_conflict else l1v["relevant"],
                "useful": True if use_conflict else l1v["useful"],
            }
            irr_labels[key] = {
                "relevant": False if rel_conflict else l1v["relevant"],
                "useful": False if use_conflict else l1v["useful"],
            }

    excluded = relevant_conflicts | useful_conflicts
    groups = _parent_session_groups(queries)

    def _variant_dict(labels: dict[tuple[str, str], dict]) -> dict:
        # covered = 候选池口径(33 候选 ∃ gold-relevant,P3 estimand)
        covered = {qid for qid in query_ids
                   if any(labels.get((qid, cid), {}).get("relevant")
                          for cid in candidates_by_query.get(qid, []))}
        uncovered = set(query_ids) - covered
        # baseline_useful = 未过滤 top5 口径(§P5 B):covered 查询的 top5(5 张卡)∃ gold-useful。
        # 剔除集卡 primary 缺 labels → .get 得 {} → 不算 useful(保守计分);sens 按翻转标签计。
        baseline_useful = {qid for qid in covered
                           if any(labels.get((qid, cid), {}).get("useful")
                                  for cid in top5_by_qid.get(qid, []))}
        return {
            "labels": labels,
            "relevant_conflicts": frozenset(relevant_conflicts),
            "useful_conflicts": frozenset(useful_conflicts),
            "excluded": frozenset(excluded),
            "covered": covered,
            "uncovered": uncovered,
            "baseline_useful": baseline_useful,
            "groups": {k: list(v) for k, v in groups.items()},
        }

    return {
        "primary": _variant_dict(primary_labels),
        "sens_rel": _variant_dict(rel_labels),
        "sens_irr": _variant_dict(irr_labels),
    }
