import json
from everos_eval.corpus import Card
from everos_eval.judge_io import build_l1_jobs, parse_verdicts


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
