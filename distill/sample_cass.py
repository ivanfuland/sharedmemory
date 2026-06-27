"""从 CASS canonical DB 过采样真实会话窗口池（供 secret 剔除后回填至 35）；会话内随机窗口；可复现。"""
import sqlite3, random
from contextlib import closing
from distill import cass_reader, filters


def _candidate_convs(canon_db):
    q = ("SELECT c.id,a.slug,MIN(m.id),MAX(m.id) FROM conversations c JOIN agents a ON a.id=c.agent_id "
         "JOIN messages m ON m.conversation_id=c.id GROUP BY c.id,a.slug")
    with closing(sqlite3.connect(f"file:{canon_db}?mode=ro", uri=True)) as db:
        return [(cid, ag, lo, hi) for (cid, ag, lo, hi) in db.execute(q).fetchall() if ag in filters.KNOWN_SOURCES]


def sample_pool(canon_db, pool_size=120, seed=20260627, max_msgs=12, min_chars=120, max_chars=4000):
    rng = random.Random(seed)
    buckets = {}
    for c in _candidate_convs(canon_db):
        buckets.setdefault(c[1], []).append(c)
    for v in buckets.values():
        rng.shuffle(v)
    order, agents = [], sorted(buckets)
    while any(buckets[a] for a in agents):
        for a in agents:
            if buckets[a]:
                order.append(buckets[a].pop())
    pool = []
    for (cid, ag, lo, hi) in order:
        if len(pool) >= pool_size:
            break
        kept, _ = filters.filter_span_messages(cass_reader.read_span_messages(canon_db, cid, lo, hi))
        if not kept:
            continue
        start = rng.randrange(len(kept)) if len(kept) > max_msgs else 0   # 会话内随机起点
        win, chars = [], 0
        for r in kept[start:start + max_msgs]:
            c = r.get("content", "") or ""
            if chars + len(c) > max_chars and win:
                break
            win.append({"idx": r["idx"], "role": r["role"], "content": c, "source_path": r.get("source_path", f"/cass/{cid}")})
            chars += len(c)
        if chars < min_chars:
            continue
        pool.append({"span": win, "split": "real", "cluster": ag, "_meta": {"conv_id": cid, "win_start": start, "agent": ag}})
    return pool


def main():
    import os, json
    pool = sample_pool(os.environ["CASS_CANON_DB"])
    json.dump(pool, open("fixtures/m4-real-pool.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"pool={len(pool)} candidates (target eval=35 after secret filter)")


if __name__ == "__main__":
    main()
