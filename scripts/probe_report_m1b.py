"""M1b probe 结果收口（Phase B Task 8,唯一入口）。人工忠实审计的数字由
--fabricated-count / --cards-reviewed 传入（人工读卡后填,脚本不自动判——忠实审计
本身是人工步骤,脚本只负责把人工读到的数字接进 §5/§6 的统计与决策管线）。
"""
from __future__ import annotations

import argparse
import json
import os

from everos_probe.report import CostSummary, FaithfulnessAudit, assemble_report, render_markdown
from everos_probe.sampling import load_snapshot


def _count_distinct_cards(outcomes: list) -> int:
    ids = set()
    for o in outcomes:
        ids.update(o.get("case_entry_ids") or [])
    return len(ids)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outcomes", required=True)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--baseline-spend", type=float, required=True)
    ap.add_argument("--final-spend", type=float, required=True)
    ap.add_argument("--sample-incomplete", action="store_true")
    ap.add_argument("--cards-reviewed", type=int, required=True)
    ap.add_argument("--fabricated-count", type=int, required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    with open(a.outcomes, encoding="utf-8") as f:
        outcomes = [json.loads(line) for line in f if line.strip()]

    manifest = load_snapshot(a.snapshot)
    w_raw = manifest["library_stratum_shares"]

    faithfulness = FaithfulnessAudit(cards_reviewed=a.cards_reviewed, fabricated_count=a.fabricated_count)
    cost = CostSummary(
        total_spend_usd=round(a.final_spend - a.baseline_spend, 6),
        cards_generated=_count_distinct_cards(outcomes),
    )
    report = assemble_report(outcomes, w_raw, faithfulness, cost, sample_incomplete=a.sample_incomplete)
    md = render_markdown(report)

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(md)

    print(md)
    print(f"\nwritten: {a.out}")


if __name__ == "__main__":
    main()
