"""probe_gold.py 的测试(P3:双源标签仲裁 —— L1 vs 统一第二判 sj)。

mini fixture(4 查询 q1..q4、6 张卡里的候选子集,每查询恰 2 候选——同构真实数据的
"每查询恰 33 候选"不变量,经 load_gold 的 expected_candidates_per_query=2 传入)手造:
- (q1, sk_1):relevant-only 冲突(L1 False/False vs SJ True/False)
- (q1, ac_1):全字段一致(True/True)
- (q2, sk_2):useful-only 冲突(L1 True/False vs SJ True/True)
- (q2, ac_2):全字段一致(True/False)
- (q3, sk_1):relevant-only 冲突;(q3, ac_1):一致 False/False
- (q4, ac_3):一致 True/True(useful!)但 **不在 q4 的 top5 里**——审查盲区专用:
  候选池含 useful 卡但 top5 没捞到,baseline_useful(§P5:未过滤 top5 口径)不得计入 q4
- (q4, sk_2):一致 True/False(在 q4 top5 里,relevant 保证 q4 covered)

q1/q2 的 external_id 共享父会话前缀(/subagents/ 之前),q3、q4 各自独立分组。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from everos_eval.probe_gold import load_gold

DATA_DIR_ENV = "EVEROS_PROBE2B_DATA"

QUERY_IDS = ("q1", "q2", "q3", "q4")
QUERY_TEXT = {"q1": "查询甲", "q2": "查询乙", "q3": "查询丙", "q4": "查询丁"}
EXTERNAL_ID = {
    "q1": "projA/sess1/subagents/agent-x.jsonl",
    "q2": "projA/sess1/subagents/agent-y.jsonl",
    "q3": "projB/sess2",
    "q4": "projC/sess3",
}
# 6 张卡(候选覆盖其中 5 张;sk_3 是从不被任何查询候选到的填充卡,用来让 L1 全笛卡尔闭合更真实)
CARDS = [
    {"card_id": "sk_1", "mem_type": "agent_skill", "title": "t", "text": "卡1正文"},
    {"card_id": "sk_2", "mem_type": "agent_skill", "title": "t", "text": "卡2正文"},
    {"card_id": "sk_3", "mem_type": "agent_skill", "title": "t", "text": "卡5正文(填充,无候选)"},
    {"card_id": "ac_1", "mem_type": "agent_case", "title": "t", "text": "卡3正文"},
    {"card_id": "ac_2", "mem_type": "agent_case", "title": "t", "text": "卡4正文"},
    {"card_id": "ac_3", "mem_type": "agent_case", "title": "t", "text": "卡6正文(useful 但 q4 top5 没捞到)"},
]
# 每查询恰 2 候选(同构真实数据"每查询恰 33"的不变量;load_gold 传 expected_candidates_per_query=2)
CANDIDATES_BY_QUERY = {
    "q1": [("sk_1", "agent_skill"), ("ac_1", "agent_case")],
    "q2": [("sk_2", "agent_skill"), ("ac_2", "agent_case")],
    "q3": [("sk_1", "agent_skill"), ("ac_1", "agent_case")],
    "q4": [("ac_3", "agent_case"), ("sk_2", "agent_skill")],
}
# 未过滤 top5(retrieval.jsonl synthetic 行的 top5 字段)——**故意不等于候选池**:
# q3 的 top5 只有 sk_1;q4 的 top5 只有 sk_2(useful 的 ac_3 在候选池但不在 top5 = 审查盲区)
TOP5_BY_QUERY = {
    "q1": [("sk_1", "agent_skill"), ("ac_1", "agent_case")],
    "q2": [("sk_2", "agent_skill"), ("ac_2", "agent_case")],
    "q3": [("sk_1", "agent_skill")],
    "q4": [("sk_2", "agent_skill")],
}
# (query_id, card_id) -> (l1_relevant, l1_useful, sj_relevant, sj_useful)
VERDICT_MATRIX = {
    ("q1", "sk_1"): (False, False, True, False),   # relevant-only 冲突
    ("q1", "ac_1"): (True, True, True, True),       # 一致
    ("q2", "sk_2"): (True, False, True, True),      # useful-only 冲突
    ("q2", "ac_2"): (True, False, True, False),     # 一致
    ("q3", "sk_1"): (False, False, True, False),    # relevant-only 冲突
    ("q3", "ac_1"): (False, False, False, False),   # 一致(不相关)
    ("q4", "ac_3"): (True, True, True, True),       # 一致 useful,但不在 q4 top5
    ("q4", "sk_2"): (True, False, True, False),     # 一致(相关不有用),在 q4 top5
}


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")


def _build_fixture(data_dir: Path, second_judge_dir: Path,
                    *, drop_sj: str | None = None, drop_l1: str | None = None,
                    drop_sj_job: str | None = None,
                    corrupt_top5: bool = False, sj_job_with_rank: bool = False,
                    sj_job_top5_prefix: bool = False) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    second_judge_dir.mkdir(parents=True, exist_ok=True)

    queryset = [
        {"query_id": qid, "external_id": EXTERNAL_ID[qid], "source": "codex", "n_rounds": 10,
         "tier": "post_cutoff", "first_user_messages": ["x"], "raw_baseline": "x",
         "query": QUERY_TEXT[qid]}
        for qid in QUERY_IDS
    ]
    _write_jsonl(data_dir / "queryset.jsonl", queryset)
    _write_jsonl(data_dir / "cards.jsonl", CARDS)

    # L1:4 查询 × 6 卡的全笛卡尔积(真实数据 30×54=1620 的缩小同构)
    card_ids = [c["card_id"] for c in CARDS]
    l1_records = []
    for qid in QUERY_IDS:
        for cid in card_ids:
            key = (qid, cid)
            if key in VERDICT_MATRIX:
                rel, use = VERDICT_MATRIX[key][0], VERDICT_MATRIX[key][1]
            else:
                rel, use = False, False  # 填充,非候选,内容不重要
            l1_records.append({"job_id": f"l1:{qid}:{cid}", "relevant": rel, "useful": use,
                               "reason": "fixture"})
    if drop_l1:
        l1_records = [r for r in l1_records if r["job_id"] != drop_l1]
    _write_jsonl(data_dir / "l1_verdicts.jsonl", l1_records)

    # retrieval.jsonl(仅 synthetic,top5 用 TOP5_BY_QUERY——**故意 ≠ 候选池**)+ top5_jobs.jsonl(冻结镜像)
    retrieval_records, top5_job_records = [], []
    for qid in QUERY_IDS:
        top5_cards = TOP5_BY_QUERY[qid]
        top5 = [{"id": cid, "mem_type": mt, "score": 0.5} for cid, mt in top5_cards]
        retrieval_records.append({"query_id": qid, "variant": "synthetic",
                                  "top5": top5, "raw_response": {}})
        for rank, (cid, mt) in enumerate(top5_cards, 1):
            top5_job_records.append({"job_id": f"top5:{qid}:{rank}:{cid}", "kind": "top5",
                                     "query": QUERY_TEXT[qid], "rank": rank, "card_id": cid,
                                     "card_type": mt, "card_text": "x"})
    _write_jsonl(data_dir / "retrieval.jsonl", retrieval_records)
    if corrupt_top5:
        top5_job_records = top5_job_records[:-1]  # 少写一条,制造派生集不相等
    _write_jsonl(data_dir / "top5_jobs.jsonl", top5_job_records)

    # second_judge/jobs.jsonl(990 全量的缩小同构:每查询恰 2 条,共 8 个候选对)
    sj_jobs = []
    for qid in QUERY_IDS:
        for cid, mt in CANDIDATES_BY_QUERY[qid]:
            job = {"job_id": f"sj:{qid}:{cid}", "kind": "sj", "query": QUERY_TEXT[qid],
                   "card_id": cid, "card_type": mt, "card_text": "x"}
            if sj_job_with_rank and qid == "q1" and cid == "sk_1":
                job["rank"] = 1
            if sj_job_top5_prefix and qid == "q1" and cid == "sk_1":
                job["job_id"] = f"top5:{qid}:1:{cid}"
            sj_jobs.append(job)
    if drop_sj_job:
        sj_jobs = [j for j in sj_jobs if j["job_id"] != drop_sj_job]
    _write_jsonl(second_judge_dir / "jobs.jsonl", sj_jobs)

    # second_judge/verdicts.jsonl
    sj_records = []
    for (qid, cid), (_, _, sj_rel, sj_use) in VERDICT_MATRIX.items():
        sj_records.append({"job_id": f"sj:{qid}:{cid}", "relevant": sj_rel, "useful": sj_use,
                           "reason": "fixture"})
    for dropped in (drop_sj, drop_sj_job):
        # drop_sj_job 同时剔除 verdict:jobs/verdicts 两边自洽地一起缺——正是"自证自洽"
        # 会静默放过、独立台账完整性校验必须抓住的场景
        if dropped:
            sj_records = [r for r in sj_records if r["job_id"] != dropped]
    _write_jsonl(second_judge_dir / "verdicts.jsonl", sj_records)


N_PER_QUERY = 2  # fixture 的"每查询恰 N 候选"不变量(真实数据 = 33)


@pytest.fixture
def gold(tmp_path):
    data_dir = tmp_path / "data"
    second_judge_dir = tmp_path / "second_judge"
    _build_fixture(data_dir, second_judge_dir)
    return load_gold(data_dir, second_judge_dir, expected_candidates_per_query=N_PER_QUERY)


def test_field_level_conflict_sets(gold):
    assert gold["primary"]["relevant_conflicts"] == {("q1", "sk_1"), ("q3", "sk_1")}
    assert gold["primary"]["useful_conflicts"] == {("q2", "sk_2")}
    assert gold["primary"]["excluded"] == {("q1", "sk_1"), ("q2", "sk_2"), ("q3", "sk_1")}
    # 三变体的冲突集/剔除集相同(仲裁规则不依赖 primary/sens 选择)
    for v in ("sens_rel", "sens_irr"):
        assert gold[v]["relevant_conflicts"] == gold["primary"]["relevant_conflicts"]
        assert gold[v]["useful_conflicts"] == gold["primary"]["useful_conflicts"]
        assert gold[v]["excluded"] == gold["primary"]["excluded"]


def test_primary_excludes_conflicted_tuples_not_counted_either_way(gold):
    primary = gold["primary"]["labels"]
    assert ("q1", "sk_1") not in primary
    assert ("q2", "sk_2") not in primary
    assert ("q3", "sk_1") not in primary
    assert primary[("q1", "ac_1")] == {"relevant": True, "useful": True}
    assert primary[("q2", "ac_2")] == {"relevant": True, "useful": False}
    assert primary[("q4", "ac_3")] == {"relevant": True, "useful": True}
    assert primary[("q4", "sk_2")] == {"relevant": True, "useful": False}


def test_sens_only_flips_the_disagreeing_field(gold):
    sens_rel, sens_irr = gold["sens_rel"]["labels"], gold["sens_irr"]["labels"]
    # relevant-only 冲突:relevant 按方向翻转,useful(两 judge 一致的 False)不动
    assert sens_rel[("q1", "sk_1")] == {"relevant": True, "useful": False}
    assert sens_irr[("q1", "sk_1")] == {"relevant": False, "useful": False}
    # useful-only 冲突:useful 按方向翻转,relevant(两 judge 一致的 True)不动
    assert sens_rel[("q2", "sk_2")] == {"relevant": True, "useful": True}
    assert sens_irr[("q2", "sk_2")] == {"relevant": True, "useful": False}
    # 无冲突元组在两个 sens 变体下与 primary 一致,不受影响
    assert sens_rel[("q1", "ac_1")] == sens_irr[("q1", "ac_1")] == {"relevant": True, "useful": True}
    assert sens_rel[("q2", "ac_2")] == sens_irr[("q2", "ac_2")] == {"relevant": True, "useful": False}


def test_covered_uncovered_per_variant(gold):
    # covered = 候选池口径(33 池的缩小同构),非 top5 口径。
    # primary:q3 候选 sk_1 被剔除、ac_1 不相关 → uncovered;q4 靠一致相关的 ac_3/sk_2 covered
    assert gold["primary"]["covered"] == {"q1", "q2", "q4"}
    assert gold["primary"]["uncovered"] == {"q3"}
    # sens_rel:(q3,sk_1) 冲突翻成 relevant=True → q3 转为 covered
    assert gold["sens_rel"]["covered"] == {"q1", "q2", "q3", "q4"}
    assert gold["sens_rel"]["uncovered"] == set()
    # sens_irr:(q3,sk_1) 翻成 relevant=False,效果与 primary 剔除相同 → 仍 uncovered
    assert gold["sens_irr"]["covered"] == {"q1", "q2", "q4"}
    assert gold["sens_irr"]["uncovered"] == {"q3"}


def test_baseline_useful_per_variant(gold):
    # baseline_useful = **未过滤 top5 口径**(§P5 B 定义),非候选池口径。
    # primary:q1 靠 top5 里的 (q1,ac_1) useful=True;q2 的 top5 = {sk_2(剔除), ac_2(useful=False)} → 不算
    assert gold["primary"]["baseline_useful"] == {"q1"}
    # sens_rel:(q2,sk_2) 的 useful 冲突翻成 True 且 sk_2 在 q2 的 top5 里 → q2 也计入
    assert gold["sens_rel"]["baseline_useful"] == {"q1", "q2"}
    # sens_irr:(q2,sk_2) 翻成 useful=False,与 primary 剔除同效 → 仍只 q1
    assert gold["sens_irr"]["baseline_useful"] == {"q1"}


def test_baseline_useful_uses_top5_not_candidate_pool(gold):
    # 审查盲区回归门:q4 的候选池含一致 useful 的 ac_3,但 q4 的 top5 只有 sk_2(不 useful)。
    # 若 baseline_useful 错用 33 候选池口径,q4 会被计入——三变体都必须排除 q4。
    for v in ("primary", "sens_rel", "sens_irr"):
        assert "q4" in gold[v]["covered"]                      # covered 仍是候选池口径
        assert "q4" not in gold[v]["baseline_useful"]           # baseline 是 top5 口径
        assert gold[v]["labels"][("q4", "ac_3")]["useful"] is True  # 前提自证:池里确有 useful 卡


def test_groups_by_parent_session(gold):
    expected = {"projA/sess1": ["q1", "q2"], "projB/sess2": ["q3"], "projC/sess3": ["q4"]}
    for v in ("primary", "sens_rel", "sens_irr"):
        assert gold[v]["groups"] == expected


def test_missing_sj_verdict_raises(tmp_path):
    data_dir, sj_dir = tmp_path / "data", tmp_path / "second_judge"
    _build_fixture(data_dir, sj_dir, drop_sj="sj:q2:ac_2")
    with pytest.raises(ValueError):
        load_gold(data_dir, sj_dir, expected_candidates_per_query=N_PER_QUERY)


def test_missing_l1_verdict_raises(tmp_path):
    data_dir, sj_dir = tmp_path / "data", tmp_path / "second_judge"
    _build_fixture(data_dir, sj_dir, drop_l1="l1:q1:sk_1")
    with pytest.raises(ValueError):
        load_gold(data_dir, sj_dir, expected_candidates_per_query=N_PER_QUERY)


def test_top5_diagnostic_closure_mismatch_raises(tmp_path):
    data_dir, sj_dir = tmp_path / "data", tmp_path / "second_judge"
    _build_fixture(data_dir, sj_dir, corrupt_top5=True)
    with pytest.raises(ValueError):
        load_gold(data_dir, sj_dir, expected_candidates_per_query=N_PER_QUERY)


def test_sj_job_with_rank_field_raises(tmp_path):
    data_dir, sj_dir = tmp_path / "data", tmp_path / "second_judge"
    _build_fixture(data_dir, sj_dir, sj_job_with_rank=True)
    with pytest.raises(ValueError):
        load_gold(data_dir, sj_dir, expected_candidates_per_query=N_PER_QUERY)


def test_sj_job_with_top5_prefix_raises(tmp_path):
    data_dir, sj_dir = tmp_path / "data", tmp_path / "second_judge"
    _build_fixture(data_dir, sj_dir, sj_job_top5_prefix=True)
    with pytest.raises(ValueError):
        load_gold(data_dir, sj_dir, expected_candidates_per_query=N_PER_QUERY)


def test_sj_jobs_file_missing_line_raises(tmp_path):
    # sj expected 集不能自证自洽:jobs.jsonl 少一行且 verdicts 同步少一行(两边自洽地缺)时,
    # 旧实现静默通过;独立完整性校验(每查询恰 N 条)必须抓住。
    data_dir, sj_dir = tmp_path / "data", tmp_path / "second_judge"
    _build_fixture(data_dir, sj_dir, drop_sj_job="sj:q2:ac_2")
    with pytest.raises(ValueError):
        load_gold(data_dir, sj_dir, expected_candidates_per_query=N_PER_QUERY)


def test_sj_jobs_query_set_mismatch_raises(tmp_path):
    # jobs.jsonl 整个查询缺失(query_id 集合 ≠ queryset)也必须 fail-loud
    data_dir, sj_dir = tmp_path / "data", tmp_path / "second_judge"
    _build_fixture(data_dir, sj_dir)
    jobs_path = sj_dir / "jobs.jsonl"
    kept = [l for l in jobs_path.read_text(encoding="utf-8").splitlines()
            if l.strip() and '"sj:q4:' not in l]
    jobs_path.write_text("\n".join(kept), encoding="utf-8")
    with pytest.raises(ValueError):
        load_gold(data_dir, sj_dir, expected_candidates_per_query=N_PER_QUERY)


# ---- Step 4:真数据入口(Step 2 盲法补判由控制面分批执行,本测试先留好可跑入口) ----

@pytest.mark.live
def test_real_data_gold_completion():
    raw = os.environ.get(DATA_DIR_ENV)
    data_dir = Path(raw) if raw else None
    if data_dir is None or not data_dir.exists():
        pytest.skip(f"set {DATA_DIR_ENV}=<probe-2b data dir> to run this live test")

    second_judge_dir = data_dir.parent / "second_judge"
    verdicts_path = second_judge_dir / "verdicts.jsonl"
    if not verdicts_path.exists():
        pytest.skip(
            f"second-judge 补判未完成(缺 {verdicts_path}):Step 2 由控制面分批执行,"
            "完成后再跑本测试拿真实冲突数/covered-uncovered/分组数"
        )

    gold = load_gold(data_dir, second_judge_dir)
    for variant in ("primary", "sens_rel", "sens_irr"):
        v = gold[variant]
        print(
            f"\n{variant}: relevant_conflicts={len(v['relevant_conflicts'])} "
            f"useful_conflicts={len(v['useful_conflicts'])} excluded={len(v['excluded'])} "
            f"covered={len(v['covered'])} uncovered={len(v['uncovered'])} "
            f"baseline_useful={len(v['baseline_useful'])}"
        )
    print(f"\ngroups: n={len(gold['primary']['groups'])}")
