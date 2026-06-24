# distill/quality_eval.py
"""Task 11: 20 样本 P/R 质量门 harness + LLM judge + 模型锁定写回。"""
import json
import sys
from datetime import datetime, timezone

from distill import distiller

_JUDGE_SCHEMA = {
    "name": "match_judge",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["matched"],
        "properties": {"matched": {"type": "integer"}},
    },
}


def match_count(gold, extracted, cfg, chat):
    """LLM judge：extracted 命中多少条 gold（语义匹配，temperature=0）。"""
    body = {
        "model": cfg["distill"]["model"],
        "temperature": 0,
        "response_format": {"type": "json_schema", "json_schema": _JUDGE_SCHEMA},
        "messages": [
            {"role": "system",
             "content": (
                 "你是语义匹配裁判。判断 extracted 列表中的每个条目是否语义上命中了 gold 列表中的某条。"
                 "一个 gold 最多被命中一次。只输出 {\"matched\": int}，int 为命中总数。"
             )},
            {"role": "user",
             "content": (
                 f"gold={json.dumps(gold, ensure_ascii=False)}\n"
                 f"extracted={json.dumps(extracted, ensure_ascii=False)}"
             )},
        ],
    }
    result = chat(body, cfg)
    return int(result["matched"])


def evaluate(cfg, eval_set, _chat=None, _judge=None):
    """对 eval_set 每条样本：蒸馏 span → LLM judge 匹配数 → 累计 P/R/F1。"""
    judge = _judge or match_count
    tot_gold = tot_ext = tot_match = 0
    per = []
    for s in eval_set:
        out = distiller.distill_span(s["span"], cfg, _chat=_chat)
        ext = [{"entity": c["entity_name"], "fact": c["fact_text"]}
               for c in out["candidates"]]
        m_raw = judge(s["gold"], ext, cfg, _chat or distiller._chat_http)
        # 限制：matched 不能超过 min(gold, extracted)，防止 LLM judge 把多条 gold 归一条 extracted（R/P 分母一致性）
        m = min(m_raw, len(s["gold"]), len(ext)) if (s["gold"] or ext) else 0
        tot_gold += len(s["gold"])
        tot_ext += len(ext)
        tot_match += m
        per.append({
            "gold": len(s["gold"]),
            "extracted": len(ext),
            "matched": m,
            "source": s["span"][0].get("source_path", "?") if s["span"] else "?",
        })
    p = tot_match / tot_ext if tot_ext else 0.0
    r = tot_match / tot_gold if tot_gold else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return {
        "precision": round(p, 3),
        "recall": round(r, 3),
        "f1": round(f1, 3),
        "n_samples": len(eval_set),
        "tot_gold": tot_gold,
        "tot_extracted": tot_ext,
        "tot_matched": tot_match,
        "per_sample": per,
    }


def gate(m):
    """PASS 条件：P≥0.9 且 R≥0.8。"""
    return m["precision"] >= 0.9 and m["recall"] >= 0.8


def lock_model(model, metrics, bridge_path="config/m3-bridge.json"):
    """gate PASS → 写 model_lock status=locked 回 bridge config。"""
    with open(bridge_path, encoding="utf-8") as f:
        d = json.load(f)
    d["model_lock"] = {
        "status": "locked",
        "model": model,
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
    }
    with open(bridge_path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def main():
    import argparse
    from distill import config

    ap = argparse.ArgumentParser(description="M3 质量门：20 样本 P/R 评估 + 模型锁定")
    ap.add_argument("--model", help="覆盖 DISTILL_MODEL（可选）")
    ap.add_argument("--lock", action="store_true", help="PASS 后写回 model_lock")
    ap.add_argument("--fixture", default="fixtures/m3-distill-eval.json",
                    help="eval fixture 路径（默认 fixtures/m3-distill-eval.json）")
    ap.add_argument("--bridge", default="config/m3-bridge.json",
                    help="bridge config 路径（默认 config/m3-bridge.json）")
    a = ap.parse_args()

    cfg = config.load()
    if a.model:
        cfg["distill"]["model"] = a.model

    with open(a.fixture, encoding="utf-8") as f:
        es = json.load(f)

    print(f"[quality_eval] model={cfg['distill']['model']}  samples={len(es)}")
    m = evaluate(cfg, es)
    print(json.dumps(m, ensure_ascii=False, indent=2))

    passed = gate(m)
    verdict = "PASS ✓" if passed else "FAIL ✗"
    print(f"\nGATE: {verdict}  (P≥0.9={m['precision']>=0.9}  R≥0.8={m['recall']>=0.8})")

    if passed and a.lock:
        lock_model(cfg["distill"]["model"], m, bridge_path=a.bridge)
        print(f"model_lock=locked ({cfg['distill']['model']}) written to {a.bridge}")
    elif passed and not a.lock:
        print("NOTE: gate PASS but --lock not given; model_lock unchanged.")
    else:
        print("model_lock remains pending_quality_gate (gate FAIL).")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
