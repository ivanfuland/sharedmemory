# distill/quality_eval.py
"""Task 11: 20 样本 P/R 质量门 harness + LLM judge + 模型锁定写回。
Task 2 升级: flat+cluster bootstrap CI + 配对非劣判据 + 欠功效门 + split 报分。
"""
import json
import os
import random
import sys
from datetime import datetime, timezone

from distill import distiller


def match_count(gold, extracted, cfg, chat):
    """LLM judge：extracted 命中多少条 gold（语义匹配，temperature=0）。"""
    body = {
        "model": os.environ.get("JUDGE_MODEL") or cfg["distill"]["model"],
        "temperature": 0,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system",
             "content": (
                 "你是语义匹配裁判。判断 extracted 列表中的每个条目是否语义上命中了 gold 列表中的某条。"
                 "一个 gold 最多被命中一次。只输出 JSON 对象 {\"matched\": int}，int 为命中总数。"
             )},
            {"role": "user",
             "content": (
                 f"gold={json.dumps(gold, ensure_ascii=False)}\n"
                 f"extracted={json.dumps(extracted, ensure_ascii=False)}"
             )},
        ],
    }
    if os.environ.get("JUDGE_BASE_URL"):   # judge 用独立 creds（公允 yardstick）
        jcfg = {**cfg, "distill": {**cfg["distill"],
                "base_url": os.environ["JUDGE_BASE_URL"], "api_key": os.environ["JUDGE_API_KEY"],
                "model": os.environ.get("JUDGE_MODEL") or cfg["distill"]["model"]}}
        result = distiller._chat_http(body, jcfg)
    else:
        result = chat(body, cfg)
    return int(result["matched"])


# ---------------------------------------------------------------------------
# Task 2: bootstrap CI helpers
# ---------------------------------------------------------------------------

def _pctl_ci(ps, rs, n_boot, alpha, extra=None):
    ps.sort(); rs.sort()
    lo = int((n_boot - 1) * (alpha / 2)); hi = int((n_boot - 1) * (1 - alpha / 2))
    out = {
        "p_lo": round(ps[lo], 3), "p_hi": round(ps[hi], 3),
        "r_lo": round(rs[lo], 3), "r_hi": round(rs[hi], 3),
        "p_mean": round(sum(ps) / n_boot, 3), "r_mean": round(sum(rs) / n_boot, 3),
        "n_boot": n_boot,
    }
    if extra:
        out.update(extra)
    return out


def bootstrap_ci(per_sample, n_boot=2000, seed=12345, alpha=0.05):
    """Flat case-resample bootstrap CI（百分位法）。"""
    rng = random.Random(seed); n = len(per_sample)
    if n == 0:
        return {"p_lo": 0.0, "p_hi": 0.0, "r_lo": 0.0, "r_hi": 0.0,
                "p_mean": 0.0, "r_mean": 0.0, "n_boot": n_boot}
    ps, rs = [], []
    for _ in range(n_boot):
        g = e = m = 0
        for _ in range(n):
            x = per_sample[rng.randrange(n)]
            g += x["gold"]; e += x["extracted"]; m += x["matched"]
        ps.append(m / e if e else 0.0); rs.append(m / g if g else 0.0)
    return _pctl_ci(ps, rs, n_boot, alpha, {"seed": seed})


def bootstrap_ci_clustered(per_sample, n_boot=2000, seed=12345, alpha=0.05):
    """簇 bootstrap：先重采样 cluster（捕获组内相关），再取被选簇全部样本。簇少→CI 宽=欠功效真信号。"""
    rng = random.Random(seed)
    clusters = {}
    for i, x in enumerate(per_sample):
        clusters.setdefault(x.get("cluster", i), []).append(x)
    keys = list(clusters)
    if not keys:
        return {"p_lo": 0.0, "p_hi": 0.0, "r_lo": 0.0, "r_hi": 0.0,
                "p_mean": 0.0, "r_mean": 0.0, "n_boot": n_boot, "n_clusters": 0}
    ps, rs = [], []
    for _ in range(n_boot):
        g = e = m = 0
        for _ in range(len(keys)):
            for x in clusters[keys[rng.randrange(len(keys))]]:
                g += x["gold"]; e += x["extracted"]; m += x["matched"]
        ps.append(m / e if e else 0.0); rs.append(m / g if g else 0.0)
    return _pctl_ci(ps, rs, n_boot, alpha, {"seed": seed, "n_clusters": len(keys)})


def paired_delta_ci(flash_per, mini_per, n_boot=2000, seed=12345, alpha=0.05):
    """配对 bootstrap：同一 real 样本上 (flash−mini) 的 P/R 差值分布（按 source 对齐）。对称，消除采样方差不对称。"""
    fm = {x["source"]: x for x in flash_per}
    mm = {x["source"]: x for x in mini_per}
    keys = [k for k in fm if k in mm]
    rng = random.Random(seed); dps, drs = [], []
    if not keys:
        return {"dp_lo": 0.0, "dp_hi": 0.0, "dr_lo": 0.0, "dr_hi": 0.0,
                "dp_mean": 0.0, "dr_mean": 0.0, "n_paired": 0, "n_boot": n_boot}
    for _ in range(n_boot):
        fe = fmt = fg = me = mmt = mg = 0
        for _ in range(len(keys)):
            k = keys[rng.randrange(len(keys))]
            f = fm[k]; mi = mm[k]
            fe += f["extracted"]; fmt += f["matched"]; fg += f["gold"]
            me += mi["extracted"]; mmt += mi["matched"]; mg += mi["gold"]
        dps.append((fmt / fe if fe else 0.0) - (mmt / me if me else 0.0))
        drs.append((fmt / fg if fg else 0.0) - (mmt / mg if mg else 0.0))
    dps.sort(); drs.sort()
    lo = int((n_boot - 1) * (alpha / 2)); hi = int((n_boot - 1) * (1 - alpha / 2))
    return {
        "dp_lo": round(dps[lo], 3), "dp_hi": round(dps[hi], 3),
        "dr_lo": round(drs[lo], 3), "dr_hi": round(drs[hi], 3),
        "dp_mean": round(sum(dps) / n_boot, 3), "dr_mean": round(sum(drs) / n_boot, 3),
        "n_paired": len(keys), "n_boot": n_boot,
    }


def gate_paired(delta, flash_ci, margin=0.05, floor_p=0.85, floor_r=0.75):
    """配对非劣判据：差值下界 ≥ −margin 且 flash 绝对地板。"""
    return (delta["dp_lo"] >= -margin and delta["dr_lo"] >= -margin
            and flash_ci["p_lo"] >= floor_p and flash_ci["r_lo"] >= floor_r)


def power_ok(clustered_ci, max_width=0.25, min_clusters=3):
    """欠功效门：簇数 ≥ min 且 cluster CI 宽 ≤ max → 有功效；否则 HOLD。"""
    w = max(clustered_ci["p_hi"] - clustered_ci["p_lo"],
            clustered_ci["r_hi"] - clustered_ci["r_lo"])
    return clustered_ci.get("n_clusters", 0) >= min_clusters and w <= max_width


def _split_of(x):
    return x.get("split") or ("synthetic" if str(x.get("source", "")).startswith("/synthetic") else "real")


def report_by_split(per_sample):
    """按 split 分组，每组返回 flat + clustered bootstrap CI。"""
    groups = {"synthetic": [], "real": []}
    for x in per_sample:
        groups[_split_of(x)].append(x)
    mk = lambda v: {"flat": bootstrap_ci(v), "clustered": bootstrap_ci_clustered(v), "n": len(v)}
    return {"overall": mk(per_sample), "synthetic": mk(groups["synthetic"]), "real": mk(groups["real"])}


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
        source = s["span"][0].get("source_path", "?") if s["span"] else "?"
        # split: real（有 agent）or synthetic（source_path 唯一）
        split = s.get("split") or ("real" if s.get("agent") else "synthetic")
        # cluster: real 用 agent id，synthetic 用 source_path
        cluster = s.get("cluster") or (s.get("agent") if s.get("agent") else (s["span"][0].get("source_path") if s["span"] else "?"))
        per.append({
            "gold": len(s["gold"]),
            "extracted": len(ext),
            "matched": m,
            "source": source,
            "split": split,
            "cluster": cluster,
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

    ap = argparse.ArgumentParser(description="M3/M6 质量门：20 样本 P/R 评估 + 配对非劣 + 模型锁定")
    ap.add_argument("--model", help="覆盖 DISTILL_MODEL（可选）")
    ap.add_argument("--lock", action="store_true", help="PASS 后写回 model_lock")
    ap.add_argument("--fixture", default="fixtures/m3-distill-eval.json",
                    help="eval fixture 路径（默认 fixtures/m3-distill-eval.json）")
    ap.add_argument("--bridge", default="config/m3-bridge.json",
                    help="bridge config 路径（默认 config/m3-bridge.json）")
    ap.add_argument("--dump-real-per", dest="dump_real_per", default=None,
                    help="存本次 real per_sample 供 flash 配对（mini 跑时使用）")
    ap.add_argument("--baseline-per", dest="baseline_per", default=None,
                    help="mini real per_sample 路径，触发配对非劣 + 功效门（flash 跑时使用）")
    a = ap.parse_args()

    cfg = config.load()
    if a.model:
        cfg["distill"]["model"] = a.model

    with open(a.fixture, encoding="utf-8") as f:
        es = json.load(f)

    print(f"[quality_eval] model={cfg['distill']['model']}  samples={len(es)}")
    m = evaluate(cfg, es)
    print(json.dumps(m, ensure_ascii=False, indent=2))

    # Task 2: split 报分
    splits = report_by_split(m["per_sample"])
    print("[by split]\n" + json.dumps(splits, ensure_ascii=False, indent=2))
    real = splits["real"]
    real_per = [x for x in m["per_sample"] if x.get("split") == "real"]

    if a.dump_real_per:
        json.dump(real_per, open(a.dump_real_per, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"[dump-real-per] {len(real_per)} samples → {a.dump_real_per}")

    passed = False
    if a.baseline_per:
        # flash 跑：配对 bootstrap (flash−mini)，对称非劣
        mini_per = json.load(open(a.baseline_per, encoding="utf-8"))
        delta = paired_delta_ci(real_per, mini_per)
        pw = power_ok(real["clustered"])
        passed = gate_paired(delta, real["flat"]) and pw
        print(f"\n[paired flash−mini] {json.dumps(delta, ensure_ascii=False)}")
        print(f"[power] clusters={real['clustered']['n_clusters']} ok={pw}")
        print(f"GATE(配对非劣 + 功效): {'PASS ✓' if passed else 'FAIL ✗'}  "
              f"dp_lo={delta['dp_lo']}≥−margin & dr_lo={delta['dr_lo']}≥−margin & flash 过地板 & power_ok={pw}")
    else:
        # 无 baseline：旧式绝对判据（后向兼容）
        passed = gate(m)
        verdict = "PASS ✓" if passed else "FAIL ✗"
        print(f"\nGATE: {verdict}  (P≥0.9={m['precision']>=0.9}  R≥0.8={m['recall']>=0.8})")
        print(f"\n[real CI] flat={real['flat']} clustered={real['clustered']}（无 --baseline-per，仅报告不锁）")

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
