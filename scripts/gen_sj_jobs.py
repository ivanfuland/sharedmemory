#!/usr/bin/env python3
"""Task 2 Step 1:生成完整 990 对统一第二判(sj)job(P3 §Task2,R5:990 全量,不复用旧 top5 判定)。

  gen_sj_jobs.py --data-dir <probe-2b/data> --out <second_judge/jobs.jsonl>

只读 --data-dir 下的 queryset.jsonl / cards.jsonl / retrieval.jsonl(variant=="synthetic"),
经 everos_eval.probe_candidates.load_candidates 逐行取 33 候选(canonical id 归一 + 硬断言),
用 everos_eval.judge_io.build_sj_jobs 逐候选建 job(无 rank、无 top5 语义),
再以 seed=20260715 的确定性 shuffle 打散写出。路径由调用方传入,不硬编码本机拓扑。
"""
from __future__ import annotations
import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from everos_eval.judge_io import build_sj_jobs
from everos_eval.probe_candidates import load_candidates

SHUFFLE_SEED = 20260715


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    data_dir = Path(a.data_dir)
    out_path = Path(a.out)
    if out_path.exists():
        sys.exit(f"{out_path} 已存在(补判台账冻结);要重跑先手动 trash")

    queries = _read_jsonl(data_dir / "queryset.jsonl")
    card_text_by_id = {c["card_id"]: c["text"] for c in _read_jsonl(data_dir / "cards.jsonl")}

    candidates_by_qid: dict[str, list[dict]] = {}
    for row in _read_jsonl(data_dir / "retrieval.jsonl"):
        if row["variant"] != "synthetic":
            continue
        candidates_by_qid[row["query_id"]] = load_candidates(row)

    missing = {q["query_id"] for q in queries} - candidates_by_qid.keys()
    if missing:
        sys.exit(f"缺 synthetic 检索行(retrieval.jsonl): {sorted(missing)}")

    jobs = build_sj_jobs(queries, candidates_by_qid, card_text_by_id)
    expected_n = len(queries) * 33
    if len(jobs) != expected_n:
        sys.exit(f"job 数不符预期: got {len(jobs)}, want {expected_n}(= {len(queries)} 查询 × 33 候选)")

    random.Random(SHUFFLE_SEED).shuffle(jobs)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(json.dumps(j, ensure_ascii=False) for j in jobs), encoding="utf-8")

    sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"{len(jobs)} sj jobs -> {out_path}")
    print(f"sha256: {sha}")


if __name__ == "__main__":
    main()
