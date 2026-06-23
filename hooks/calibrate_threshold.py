#!/usr/bin/env python3
"""扫阈值候选，对标注集算 precision/recall，挑「漏注优于污染」的保守阈值。
precision 优先（宁高勿低）：在 precision>=0.9 的候选里取召回最高者；都达不到则取 precision 最高的最低阈值。"""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import gbrain_digest as gd

CANDIDATES = [round(x / 100, 2) for x in range(50, 96, 5)]  # 0.50..0.95


def evaluate(labeled, gbrain_home=None):
    rows = []
    per_query = []
    for item in labeled["queries"]:
        raw = gd._run_query(item["q"], gbrain_home=gbrain_home)
        hits = gd.parse_query(raw)
        per_query.append((item, hits))
    for th in CANDIDATES:
        tp = fp = fn = 0
        for item, hits in per_query:
            got = {s for sc, s, _ in hits if sc >= th}
            rel = set(item.get("relevant", []))
            tp += len(got & rel); fp += len(got - rel); fn += len(rel - got)
        prec = tp / (tp + fp) if (tp + fp) else 1.0
        rec = tp / (tp + fn) if (tp + fn) else 1.0
        rows.append({"threshold": th, "precision": round(prec, 3), "recall": round(rec, 3),
                     "tp": tp, "fp": fp, "fn": fn})
    return rows


MIN_POS_LABELS = 5    # 内容不足下限：少于这些正例标签 = 标定无意义


def pick(rows, n_pos, max_tp):
    # 内容不足（正例标签太少 / 任何阈值都没命中过真正例）→ 不假装标定，退保守默认
    if n_pos < MIN_POS_LABELS or max_tp == 0:
        return gd.DEFAULT_THRESHOLD, "uncalibrated_default", (
            f"内容不足（n_pos={n_pos}<{MIN_POS_LABELS} 或 max_tp={max_tp}=0）→ 退保守默认 "
            f"{gd.DEFAULT_THRESHOLD}，不称已标定；P4 内容灌入后重跑")
    hi = [r for r in rows if r["precision"] >= 0.9]
    if hi:
        best = max(hi, key=lambda r: (r["recall"], -r["threshold"]))
        return best["threshold"], "calibrated", f"precision>=0.9 候选取召回最高: {best}"
    best = max(rows, key=lambda r: (r["precision"], -r["threshold"]))
    return best["threshold"], "calibrated_low_precision", f"无 precision>=0.9，取 precision 最高最低阈: {best}"


if __name__ == "__main__":
    here = os.path.dirname(__file__)
    labeled = json.load(open(os.path.join(here, "..", "fixtures", "threshold-labeled-set.json")))
    rows = evaluate(labeled, gbrain_home=os.environ.get("GBRAIN_HOME"))
    n_pos = sum(len(q.get("relevant", [])) for q in labeled["queries"])
    max_tp = max((r["tp"] for r in rows), default=0)
    th, status, reason = pick(rows, n_pos, max_tp)
    cfg = {"query_threshold": th, "max_inject_tokens": 1500, "status": status, "method": reason,
           "labeled_n": len(labeled["queries"]), "positive_labels": n_pos, "max_tp": max_tp,
           "calibrated_against": "M2-seed-corpus", "recalibrate_after": "P4", "sweep": rows}
    os.makedirs(os.path.join(here, "..", "config"), exist_ok=True)
    json.dump(cfg, open(os.path.join(here, "..", "config", "m2-thresholds.json"), "w"),
              ensure_ascii=False, indent=2)
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
