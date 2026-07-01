# tests/test_cass_corpus_incremental.py
import os
import sqlite3
import pytest
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


# ---- reader: 严格 keyset 复合游标 ----

def test_since_none_is_newest_desc(tmp_path):
    p = str(tmp_path / "c.db")
    _mk_db(p, [(1, "a", 100, 6), (2, "a", 300, 6), (3, "a", 200, 6)])
    rows = reader.select_conversations(p, limit=2, since_cursor=None)
    assert [r["id"] for r in rows] == [2, 3]   # newest-first DESC, top 2


def test_since_cursor_strict_keyset(tmp_path):
    p = str(tmp_path / "c.db")
    _mk_db(p, [(1, "a", 100, 6), (2, "a", 300, 6), (3, "a", 200, 6)])
    rows = reader.select_conversations(p, limit=10, since_cursor=(200, 3))
    assert [r["id"] for r in rows] == [2]      # 严格 >(200,3): 只有 (300,2)


def test_since_cursor_same_ts_higher_id(tmp_path):
    # P0 核心:同 ts 用 id 做次级 key,不会漏
    p = str(tmp_path / "c.db")
    _mk_db(p, [(1, "a", 100, 6), (2, "a", 200, 6), (3, "a", 200, 6)])
    rows = reader.select_conversations(p, limit=10, since_cursor=(200, 2))
    assert [r["id"] for r in rows] == [3]      # 同 ts=200 且 id>2 → id3


def test_since_cursor_respects_cap_asc(tmp_path):
    p = str(tmp_path / "c.db")
    _mk_db(p, [(1, "a", 100, 6), (2, "a", 300, 6), (3, "a", 200, 6)])
    rows = reader.select_conversations(p, limit=1, since_cursor=(0, 0))
    assert [r["id"] for r in rows] == [1]      # cap=1, (ts,id) ASC → (100,1)


def test_max_conversation_cursor(tmp_path):
    p = str(tmp_path / "c.db")
    _mk_db(p, [(1, "a", 100, 6), (2, "a", 300, 6), (5, "a", 300, 6)])
    assert reader.max_conversation_cursor(p) == (300, 5)   # 最大 (ts,id)


def test_max_conversation_cursor_empty(tmp_path):
    p = str(tmp_path / "c.db")
    _mk_db(p, [])
    assert reader.max_conversation_cursor(p) is None


# ---- run_feed: 首跑播种 / 增量零丢失 / wedge / 坏文件 ----

def test_run_feed_first_run_seeds_only_no_import(tmp_path):
    """首跑(codex P1-A fix a):只播种游标=最新,import 0(不 courtesy 导)。"""
    db = str(tmp_path / "c.db"); out = str(tmp_path / "out"); os.makedirs(out)
    sp = str(tmp_path / "wm.json")
    _mk_db(db, [(i, "a", i * 100, 6) for i in range(1, 6)])   # ts100..500, id1..5
    r = export.run_feed(db, out, cap=2, state_path=sp)
    assert r["total"] == 0 and len(r["written"]) == 0        # import 0
    assert state.load_cursor(sp) == (500, 5)                  # 播种=最新 (ts500,id5)
    assert [f for f in os.listdir(out) if f.endswith(".md")] == []


def test_run_feed_incremental_drains_all_no_drop(tmp_path):
    """播种后从头 drain,cap 分多轮,全部导出零丢失,游标单调推进。"""
    db = str(tmp_path / "c.db"); out = str(tmp_path / "out"); os.makedirs(out)
    sp = str(tmp_path / "wm.json")
    _mk_db(db, [(i, "a", i * 100, 6) for i in range(1, 6)])
    state.save_cursor(sp, 0, 0)                               # 从头(等价 backfill)
    for _ in range(5):
        export.run_feed(db, out, cap=2, state_path=sp)
    got = {f for f in os.listdir(out) if f.endswith(".md")}
    assert len(got) == 5
    assert state.load_cursor(sp) == (500, 5)


def test_run_feed_wedge_same_ts_progresses(tmp_path):
    """P0 复现:≥cap 条会话同一 ts 时,复合游标必须能推进过去(旧 >=ts 会 wedge、id 大的永不导)。"""
    db = str(tmp_path / "c.db"); out = str(tmp_path / "out"); os.makedirs(out)
    sp = str(tmp_path / "wm.json")
    _mk_db(db, [(1, "a", 100, 6), (2, "a", 100, 6), (3, "a", 100, 6),
                (4, "a", 100, 6), (5, "a", 200, 6)])          # 4 条同 ts=100 + 1 条 ts=200
    state.save_cursor(sp, 0, 0)
    for _ in range(6):                                        # cap=2 → 多轮
        export.run_feed(db, out, cap=2, state_path=sp)
    got = {f for f in os.listdir(out) if f.endswith(".md")}
    assert len(got) == 5                                      # 5 条全导出(旧 >= 卡死 → 只 4 条)
    assert state.load_cursor(sp) == (200, 5)


def test_run_feed_corrupt_state_raises(tmp_path):
    """codex P1-B:坏水位线文件 → fail loud,不静默重播种跳过 backlog。"""
    db = str(tmp_path / "c.db"); out = str(tmp_path / "out"); os.makedirs(out)
    sp = tmp_path / "wm.json"; sp.write_text("garbage{")
    _mk_db(db, [(1, "a", 100, 6)])
    with pytest.raises(Exception):
        export.run_feed(db, out, cap=2, state_path=str(sp))
