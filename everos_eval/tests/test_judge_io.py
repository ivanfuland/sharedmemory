import json
from everos_eval.corpus import Card
from everos_eval.judge_io import build_l1_jobs, build_sj_jobs, parse_verdicts


def test_build_l1_jobs_cartesian():
    qs = [{"query_id": "q1", "query": "合成查询"}]
    cards = [Card("sk_1", "agent_skill", "t", "body"), Card("ac_1", "agent_case", "t", "body")]
    jobs = build_l1_jobs(qs, cards)
    assert {j["job_id"] for j in jobs} == {"l1:q1:sk_1", "l1:q1:ac_1"}
    assert all(j["kind"] == "l1" and j["card_text"] == "body" for j in jobs)


def test_parse_verdicts_per_candidate_filtering(tmp_path):
    p = tmp_path / "v.jsonl"
    lines = [
        {"job_id": "l1:q1:sk_1", "relevant": True, "useful": True, "reason": "ok"},
        {"job_id": "l1:q1:ac_1", "relevant": "yes", "useful": False, "reason": "bad-type"},  # 坏行
        {"job_id": "l1:q1:ac_2", "relevant": False, "useful": True, "reason": "违反约束"},   # useful 无 relevant
        {"job_id": "top5:q1:1:x", "relevant": True, "useful": False, "reason": "错 kind"},   # 前缀不符
        {"job_id": "l1:q1:sk_1", "relevant": True, "useful": False, "reason": "重复"},       # dup
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    expected = {"l1:q1:sk_1", "l1:q1:ac_1", "l1:q1:ac_2", "l1:q1:ac_3"}
    ok, failed = parse_verdicts(p, "l1", expected_job_ids=expected)
    assert set(ok) == {"l1:q1:sk_1"}
    assert {"l1:q1:ac_1", "l1:q1:ac_2"} <= set(failed)          # 坏行重试,不静默丢
    assert any(f.startswith("wrong_kind:") for f in failed)      # 错文件防呆
    assert any(f.startswith("duplicate:") for f in failed)       # 重复覆盖防呆
    assert "missing:l1:q1:ac_3" in failed                        # 漏判防呆


# ---- P3 §Task2 Step1:统一第二判(sj)job 生成——逐候选、无 rank、无 top5 语义 ----

def test_build_sj_jobs_per_candidate_no_rank():
    qs = [{"query_id": "q1", "query": "合成查询"}, {"query_id": "q2", "query": "另一条查询"}]
    candidates_by_qid = {
        "q1": [
            {"canonical_card_id": "sk_1", "mem_type": "agent_skill", "source_rank": 0,
             "native_score": 0.9, "payload": {}},
            {"canonical_card_id": "ac_1", "mem_type": "agent_case", "source_rank": 0,
             "native_score": 0.5, "payload": {}},
        ],
        "q2": [
            {"canonical_card_id": "sk_1", "mem_type": "agent_skill", "source_rank": 0,
             "native_score": 0.7, "payload": {}},
        ],
    }
    card_text_by_id = {"sk_1": "技能正文", "ac_1": "案例正文"}
    jobs = build_sj_jobs(qs, candidates_by_qid, card_text_by_id)
    assert {j["job_id"] for j in jobs} == {"sj:q1:sk_1", "sj:q1:ac_1", "sj:q2:sk_1"}
    for j in jobs:
        assert j["kind"] == "sj"
        assert "rank" not in j                        # 无 rank 字段(与 top5 job 区分)
        assert not j["job_id"].startswith("top5:")     # 无 top5 语义
        assert set(j.keys()) == {"job_id", "kind", "query", "card_id", "card_type", "card_text"}
    q1_sk1 = next(j for j in jobs if j["job_id"] == "sj:q1:sk_1")
    assert q1_sk1["query"] == "合成查询" and q1_sk1["card_type"] == "agent_skill"
    assert q1_sk1["card_text"] == "技能正文"


def test_parse_verdicts_sj_kind_uses_sj_prefix():
    # judge_io 通用 parse_verdicts 对新 kind="sj" 同样按前缀/坏行/漏判过滤,不需要专门改 parse_verdicts 本身
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "v.jsonl"
        lines = [
            {"job_id": "sj:q1:sk_1", "relevant": True, "useful": True, "reason": "ok"},
            {"job_id": "l1:q1:sk_1", "relevant": True, "useful": True, "reason": "错 kind"},
        ]
        p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        ok, failed = parse_verdicts(p, "sj", expected_job_ids={"sj:q1:sk_1", "sj:q1:ac_1"})
        assert set(ok) == {"sj:q1:sk_1"}
        assert any(f.startswith("wrong_kind:") for f in failed)
        assert "missing:sj:q1:ac_1" in failed
