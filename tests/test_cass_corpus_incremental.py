# tests/test_cass_corpus_incremental.py
import os
import sqlite3
from cass_corpus import reader, export, state


def _mk_db(path, convs):
    """convs: list of (id, agent_slug, last_ts, n_msgs). 全合成数据(PUBLIC 仓隐私)。
    每条 message 内容刻意造长(~3k 字符),让渲染后 transcript 稳过 export 的 min_chars=2000。"""
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE agents(id INTEGER PRIMARY KEY, slug TEXT);"
        "CREATE TABLE workspaces(id INTEGER PRIMARY KEY, path TEXT);"
        "CREATE TABLE conversations(id INTEGER PRIMARY KEY, title TEXT, workspace_id INT,"
        " source_path TEXT, started_at INT, last_message_created_at INT, agent_id INT, primary_model TEXT);"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INT, idx INT, role TEXT, content TEXT);"
    )
    db.execute("INSERT INTO agents VALUES(1,'test-agent')")
    mid = 1
    for cid, slug, last_ts, n in convs:
        db.execute("INSERT OR IGNORE INTO agents VALUES(?,?)", (cid + 100, slug))
        db.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?)",
            (cid, f"t{cid}", None, None, last_ts, last_ts, cid + 100, "m"),
        )
        for i in range(n):
            body = f"turn {i} " + "lorem ipsum dolor sit amet consectetur " * 80  # ~3k 字符, 唯一
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?)", (mid, cid, i, "user", body))
            mid += 1
    db.commit()
    db.close()


def test_since_none_is_newest_desc(tmp_path):
    p = str(tmp_path / "c.db")
    _mk_db(p, [(1, "a", 100, 6), (2, "a", 300, 6), (3, "a", 200, 6)])
    rows = reader.select_conversations(p, limit=2, since_ts=None)
    assert [r["id"] for r in rows] == [2, 3]   # newest-first, top 2


def test_since_ts_incremental_asc(tmp_path):
    p = str(tmp_path / "c.db")
    _mk_db(p, [(1, "a", 100, 6), (2, "a", 300, 6), (3, "a", 200, 6)])
    rows = reader.select_conversations(p, limit=10, since_ts=200)
    assert [r["id"] for r in rows] == [3, 2]   # >=200, ASC: 200 then 300


def test_since_ts_respects_cap_oldest_first(tmp_path):
    p = str(tmp_path / "c.db")
    _mk_db(p, [(1, "a", 100, 6), (2, "a", 300, 6), (3, "a", 200, 6)])
    rows = reader.select_conversations(p, limit=1, since_ts=0)
    assert [r["id"] for r in rows] == [1]      # cap=1, oldest (ts=100) first


def test_max_conversation_ts(tmp_path):
    p = str(tmp_path / "c.db")
    _mk_db(p, [(1, "a", 100, 6), (2, "a", 300, 6)])
    assert reader.max_conversation_ts(p) == 300


def test_max_conversation_ts_empty(tmp_path):
    p = str(tmp_path / "c.db")
    _mk_db(p, [])
    assert reader.max_conversation_ts(p) is None


def test_run_feed_incremental_no_drop_busy_day(tmp_path):
    """忙日 >cap 也不丢:两轮把全部增量 drain 完,水位线单调推进。"""
    db = str(tmp_path / "c.db")
    out = str(tmp_path / "out"); os.makedirs(out)
    sp = str(tmp_path / "wm.json")
    _mk_db(db, [(i, "a", i * 100, 6) for i in range(1, 6)])   # ts 100..500
    state.save_watermark(sp, 0)                               # 播种 0 = 全量增量
    r1 = export.run_feed(db, out, cap=2, state_path=sp)       # >=0 ASC cap2 → ts100,200
    assert r1["max_ts"] == 200 and state.load_watermark(sp) == 200
    r2 = export.run_feed(db, out, cap=2, state_path=sp)       # >=200 ASC cap2 → ts200,300
    assert state.load_watermark(sp) == 300
    for _ in range(3):
        export.run_feed(db, out, cap=2, state_path=sp)
    got = {f for f in os.listdir(out) if f.endswith(".md")}
    assert len(got) == 5                                      # 全部 5 会话最终都导出,零丢失


def test_run_feed_first_run_seeds_no_backfill(tmp_path):
    """首跑无水位线:播种=当前 max ts,只导最新 N,不灌存量。"""
    db = str(tmp_path / "c.db"); out = str(tmp_path / "out"); os.makedirs(out)
    sp = str(tmp_path / "wm.json")   # 不存在 → load 返回 None
    _mk_db(db, [(i, "a", i * 100, 6) for i in range(1, 6)])
    r = export.run_feed(db, out, cap=2, state_path=sp)   # 首跑
    assert state.load_watermark(sp) == 500               # 播种=全库 max
    assert len(r["written"]) <= 2                         # 只导最新 cap 条(courtesy),不 backfill
