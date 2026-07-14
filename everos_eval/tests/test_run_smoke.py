"""集成 smoke:真 subprocess 跑 scripts/eval_run_m1c.py assemble 到收尾(MEMORY 教训:helper
单测≠集成正确,被测物是脚本/集成流程时至少一个测试要真 exec 它、断言走完关键路径)。
workdir 全合成,不含任何真实语料/私密数据。
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "eval_run_m1c.py"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")


def _build_synthetic_workdir(wd: Path) -> None:
    # queryset:2 条合成查询
    _write_jsonl(wd / "queryset.jsonl", [
        {"query_id": "q01", "external_id": "syn-ext-1", "source": "synthetic",
         "n_rounds": 6, "tier": "post_cutoff", "first_user_messages": ["合成用户消息甲"],
         "raw_baseline": "合成用户消息甲", "query": "合成查询甲"},
        {"query_id": "q02", "external_id": "syn-ext-2", "source": "synthetic",
         "n_rounds": 7, "tier": "post_cutoff", "first_user_messages": ["合成用户消息乙"],
         "raw_baseline": "合成用户消息乙", "query": "合成查询乙"},
    ])

    # retrieval:每条 query 各一条 synthetic + 一条 raw(top5 只需 id 字段供 assemble 消费)
    _write_jsonl(wd / "retrieval.jsonl", [
        {"query_id": "q01", "variant": "synthetic",
         "top5": [{"id": "sk_1", "mem_type": "agent_skill", "score": 0.9},
                  {"id": "ac_1", "mem_type": "agent_case", "score": 0.5}],
         "raw_response": {}},
        {"query_id": "q01", "variant": "raw",
         "top5": [{"id": "ac_1", "mem_type": "agent_case", "score": 0.4}],
         "raw_response": {}},
        {"query_id": "q02", "variant": "synthetic",
         "top5": [{"id": "sk_1", "mem_type": "agent_skill", "score": 0.8},
                  {"id": "ac_1", "mem_type": "agent_case", "score": 0.3}],
         "raw_response": {}},
        {"query_id": "q02", "variant": "raw",
         "top5": [{"id": "sk_1", "mem_type": "agent_skill", "score": 0.2}],
         "raw_response": {}},
    ])

    # l1 jobs + verdicts:job_id = l1:{query_id}:{card_id}
    _write_jsonl(wd / "l1_jobs.jsonl", [
        {"job_id": "l1:q01:sk_1", "kind": "l1", "query": "合成查询甲", "card_id": "sk_1",
         "card_type": "agent_skill", "card_text": "合成技能甲正文"},
        {"job_id": "l1:q01:ac_1", "kind": "l1", "query": "合成查询甲", "card_id": "ac_1",
         "card_type": "agent_case", "card_text": "合成案例甲正文"},
        {"job_id": "l1:q02:sk_1", "kind": "l1", "query": "合成查询乙", "card_id": "sk_1",
         "card_type": "agent_skill", "card_text": "合成技能甲正文"},
        {"job_id": "l1:q02:ac_1", "kind": "l1", "query": "合成查询乙", "card_id": "ac_1",
         "card_type": "agent_case", "card_text": "合成案例甲正文"},
    ])
    _write_jsonl(wd / "l1_verdicts.jsonl", [
        {"job_id": "l1:q01:sk_1", "relevant": True, "useful": True, "reason": "合成:命中"},
        {"job_id": "l1:q01:ac_1", "relevant": False, "useful": False, "reason": "合成:未命中"},
        {"job_id": "l1:q02:sk_1", "relevant": True, "useful": False, "reason": "合成:相关但不够用"},
        {"job_id": "l1:q02:ac_1", "relevant": False, "useful": False, "reason": "合成:未命中"},
    ])

    # top5 jobs + verdicts:job_id = top5:{query_id}:{rank}:{card_id}(仅 synthetic 变体)
    _write_jsonl(wd / "top5_jobs.jsonl", [
        {"job_id": "top5:q01:1:sk_1", "kind": "top5", "query": "合成查询甲", "rank": 1,
         "card_id": "sk_1", "card_type": "agent_skill", "card_text": "合成技能甲正文"},
        {"job_id": "top5:q01:2:ac_1", "kind": "top5", "query": "合成查询甲", "rank": 2,
         "card_id": "ac_1", "card_type": "agent_case", "card_text": "合成案例甲正文"},
        {"job_id": "top5:q02:1:sk_1", "kind": "top5", "query": "合成查询乙", "rank": 1,
         "card_id": "sk_1", "card_type": "agent_skill", "card_text": "合成技能甲正文"},
        {"job_id": "top5:q02:2:ac_1", "kind": "top5", "query": "合成查询乙", "rank": 2,
         "card_id": "ac_1", "card_type": "agent_case", "card_text": "合成案例甲正文"},
    ])
    _write_jsonl(wd / "top5_verdicts.jsonl", [
        {"job_id": "top5:q01:1:sk_1", "relevant": True, "useful": True, "reason": "合成:命中"},
        {"job_id": "top5:q01:2:ac_1", "relevant": False, "useful": False, "reason": "合成:未命中"},
        {"job_id": "top5:q02:1:sk_1", "relevant": True, "useful": False, "reason": "合成:相关但不够用"},
        {"job_id": "top5:q02:2:ac_1", "relevant": False, "useful": False, "reason": "合成:未命中"},
    ])

    # foresight jobs + verdicts:job_id = fs:{entry_id}
    _write_jsonl(wd / "foresight_jobs.jsonl", [
        {"job_id": "fs:fs_1", "kind": "foresight", "entry_text": "合成前瞻条目甲"},
        {"job_id": "fs:fs_2", "kind": "foresight", "entry_text": "合成前瞻条目乙"},
    ])
    _write_jsonl(wd / "foresight_verdicts.jsonl", [
        {"job_id": "fs:fs_1", "category": "insight", "reason": "合成:有洞察"},
        {"job_id": "fs:fs_2", "category": "trivial", "reason": "合成:琐碎"},
    ])


def test_assemble_runs_end_to_end(tmp_path):
    wd = tmp_path / "eval-workdir"
    wd.mkdir()
    _build_synthetic_workdir(wd)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "assemble", "--workdir", str(wd)],
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    metrics_path = wd / "metrics.json"
    assert metrics_path.exists()
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert "main_gate_verdict" in metrics
    assert "foresight_noise_ratio" in metrics
    # 2 条合成 foresight,1 条 trivial -> noise_ratio = 0.5(必需诊断项非静默缺)
    assert metrics["foresight_noise_ratio"] == 0.5
