import json, os, sqlite3, statistics, subprocess, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

EMBED_BASE = os.environ["OPENROUTER_BASE_URL"].rstrip("/")   # LiteLLM /v1（与 gbrain 嵌入同端点同路由）
EMBED_KEY = os.environ["OPENROUTER_API_KEY"]
CANON = os.environ["CASS_CANON_DB"]
FIELDS = json.load(open(os.path.join(os.path.dirname(__file__), "..", "contracts", "cass-canonical-fields.json")))

def _pctl(xs, p): xs = sorted(xs); return round(xs[min(len(xs)-1, int(len(xs)*p))], 4)

def sample_corpus(n=300, min_days=7):
    """M0 read_sql JOIN：≥7 天时间窗 + 均匀抽样 ≤n（注：嵌入会送 OpenAI，n 取小控成本/暴露）。"""
    con = sqlite3.connect(f"file:{CANON}?mode=ro", uri=True)
    max_ts = int(con.execute("SELECT max(created_at) FROM messages").fetchone()[0])
    floor_ts = max_ts - int((min_days + 1) * 86400 * 1000)
    cur = con.execute(FIELDS["read_sql"] + " WHERE m.created_at >= ? ORDER BY m.id ASC", (floor_ts,))
    rows = cur.fetchall(); cols = [d[0] for d in cur.description]; con.close()
    recs = [dict(zip(cols, r)) for r in rows]
    assert recs, f"近 {min_days} 天无消息"
    for a in FIELDS["required_aliases"]:
        assert all(r[a] not in (None, "") for r in recs), f"必需字段 {a} 有空（违反 M0 读契约）"
    ts = [int(r["timestamp"]) for r in recs]
    span = (max(ts) - min(ts)) / 1000 / 86400
    assert span >= min_days, f"corpus 跨度 {span:.1f}d < {min_days}d"
    if len(recs) > n:
        step = len(recs) / n
        recs = [recs[int(i * step)] for i in range(n)]
    # 截断到 ~6000 字符：text-embedding-3-small 上限 8191 token；少数超长消息（工具/代码 dump，
    # 实测样本 max 31w 字符）会撞上限 → 400。基准只量吞吐故截断；M3 蒸馏桥须对长消息分块（记 M1-EXIT）
    return [r["content"][:6000] for r in recs], round(span, 1)

def _embed(texts):
    body = {"model": "text-embedding-3-small", "input": texts, "dimensions": 1536}   # 经 LiteLLM → OpenAI
    req = urllib.request.Request(f"{EMBED_BASE}/embeddings", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {EMBED_KEY}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)["data"]
    assert all(len(d["embedding"]) == 1536 for d in data), "嵌入维度 != 1536（LiteLLM 没透传 dimensions？）"
    return [d["embedding"] for d in data]

rec_all = []
def bench_embedding(texts):
    _embed(texts[:4])                                    # warmup
    best = None
    for batch in (16, 32, 64):
        lat, total = [], 0
        for i in range(0, len(texts), batch):
            chunk = texts[i:i+batch]; t = time.perf_counter()
            _embed(chunk); lat.append(time.perf_counter() - t); total += len(chunk)
        rec = {"batch": batch, "n": total, "samples": len(lat), "warmup": 1,
               "embeds_per_s": round(total/sum(lat), 1),
               "p50_batch_s": _pctl(lat, .5), "p95_batch_s": _pctl(lat, .95)}
        if not best or rec["embeds_per_s"] > best["embeds_per_s"]: best = rec
        rec_all.append(rec)
    return best

def bench_distill():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "infra", "distill"))
    import smoke
    def timed(_):
        t = time.perf_counter()
        try:
            smoke.distill_once(audit_path=os.devnull); return (time.perf_counter()-t, True)
        except Exception:
            return (time.perf_counter()-t, False)
    timed(0)
    out = {}
    for conc in (1, 2, 4):
        with ThreadPoolExecutor(conc) as ex:
            res = list(ex.map(timed, range(conc*4)))
        lat = [d for d, ok in res if ok]; errs = sum(1 for _, ok in res if not ok)
        out[f"conc{conc}"] = {"samples": len(res), "errors": errs, "error_rate": round(errs/len(res), 3),
                              "p50_s": _pctl(lat or [0.0], .5), "p95_s": _pctl(lat or [0.0], .95)}
    return out

def pick_distill_concurrency(dis):
    base = dis["conc1"]["p95_s"] or 0.0
    best, reason = 1, f"默认 conc1（p95={base}s）"
    for conc in (2, 4):
        d = dis[f"conc{conc}"]
        if d["error_rate"] == 0 and (base == 0 or d["p95_s"] <= 2 * base):
            best, reason = conc, f"conc{conc}: error_rate=0 且 p95={d['p95_s']}s ≤ 2×conc1({base}s)"
        else:
            reason = f"止于 conc{best}（conc{conc} error_rate={d['error_rate']}/p95={d['p95_s']}s 超阈）"
            break
    return best, reason

if __name__ == "__main__":
    texts, span = sample_corpus()
    emb = bench_embedding(texts)
    dis = bench_distill()
    dc, reason = pick_distill_concurrency(dis)
    timeout = max(90, int((dis[f"conc{dc}"]["p95_s"] or 30.0) * 3))
    result = {
        "corpus_span_days": span, "embedding": {**emb, "dim": 1536, "provider": "litellm/text-embedding-3-small", "sweep": rec_all},
        "distill": dis,
        "derived_config": {"embed_batch_size": emb["batch"],
                           "distill_concurrency": dc, "distill_concurrency_reason": reason, "distill_timeout_s": timeout}}
    os.makedirs("config", exist_ok=True)
    json.dump(result, open("config/m1-benchmarks.json", "w"), ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
