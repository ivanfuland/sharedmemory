# tests/test_export_by_external_id.py
# adapter 按 external_id（稳定键）导出。全合成数据（PUBLIC 仓隐私）。TDD：先失败。
import sqlite3

import pytest

from cass_corpus import export, reader


def _mk_db_eid(path, convs):
    """convs: list of (id, slug, external_id, last_ts, n_msgs)。带 external_id/source_id 列的新 schema。"""
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE agents(id INTEGER PRIMARY KEY, slug TEXT);"
        "CREATE TABLE workspaces(id INTEGER PRIMARY KEY, path TEXT);"
        "CREATE TABLE conversations(id INTEGER PRIMARY KEY, title TEXT, workspace_id INT,"
        " source_path TEXT, external_id TEXT, source_id TEXT,"
        " started_at INT, last_message_created_at INT, agent_id INT, primary_model TEXT);"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INT, idx INT, role TEXT,"
        " content TEXT, created_at INT);"
    )
    mid = 1
    for cid, slug, eid, last_ts, n in convs:
        db.execute("INSERT OR IGNORE INTO agents VALUES(?,?)", (cid + 100, slug))
        db.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cid, f"t{cid}", None, None, eid, f"src-{cid}", last_ts, last_ts, cid + 100, "m"),
        )
        for i in range(n):
            body = f"turn {i} " + ("lorem ipsum dolor sit amet " * 30)
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",
                       (mid, cid, i, "user", body, last_ts - (n - 1 - i) * 1000))
            mid += 1
    db.commit()
    db.close()


def test_get_by_external_id_hit(tmp_path):
    dbp = str(tmp_path / "c.db")
    _mk_db_eid(dbp, [(1, "codex", "eid-aaa", 1000_000, 5), (2, "codex", "eid-bbb", 2000_000, 5)])
    meta = reader.get_conversation_by_external_id(dbp, "eid-bbb")
    assert meta is not None and meta["id"] == 2 and meta["external_id"] == "eid-bbb"


def test_get_by_external_id_miss_returns_none(tmp_path):
    dbp = str(tmp_path / "c.db")
    _mk_db_eid(dbp, [(1, "codex", "eid-aaa", 1000_000, 5)])
    assert reader.get_conversation_by_external_id(dbp, "eid-nope") is None


def test_get_by_external_id_duplicate_fails_loud(tmp_path):
    dbp = str(tmp_path / "c.db")
    _mk_db_eid(dbp, [(1, "codex", "eid-dup", 1000_000, 5), (2, "openclaw", "eid-dup", 2000_000, 5)])
    with pytest.raises(RuntimeError, match="eid-dup"):
        reader.get_conversation_by_external_id(dbp, "eid-dup")


def test_get_by_external_id_legacy_schema_fails_loud(tmp_path):
    """老/合成 schema 无 external_id 列 → 显式 raise，不静默返回 None。"""
    dbp = str(tmp_path / "legacy.db")
    db = sqlite3.connect(dbp)
    db.executescript(
        "CREATE TABLE agents(id INTEGER PRIMARY KEY, slug TEXT);"
        "CREATE TABLE workspaces(id INTEGER PRIMARY KEY, path TEXT);"
        "CREATE TABLE conversations(id INTEGER PRIMARY KEY, title TEXT, workspace_id INT,"
        " source_path TEXT, started_at INT, last_message_created_at INT, agent_id INT, primary_model TEXT);"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INT, idx INT, role TEXT,"
        " content TEXT, created_at INT);"
    )
    db.commit(); db.close()
    with pytest.raises(RuntimeError, match="external_id"):
        reader.get_conversation_by_external_id(dbp, "eid-x")


def test_export_one_by_external_id_writes_transcript(tmp_path):
    dbp = str(tmp_path / "c.db"); out = str(tmp_path / "out")
    _mk_db_eid(dbp, [(7, "codex", "eid-ccc", 3000_000, 6)])
    rep = export.export_one(dbp, out, external_id="eid-ccc", min_chars=10)
    assert len(rep["written"]) == 1 and rep["errors"] == []
    assert rep["exported_ts"] == 3000_000


def test_export_one_by_external_id_miss_is_skipped_shape(tmp_path):
    dbp = str(tmp_path / "c.db"); out = str(tmp_path / "out")
    _mk_db_eid(dbp, [(7, "codex", "eid-ccc", 3000_000, 6)])
    rep = export.export_one(dbp, out, external_id="eid-nope", min_chars=10)
    assert rep == {"written": [], "skipped": [], "errors": [], "total": 0, "exported_ts": None}


def test_export_one_selector_exclusive(tmp_path):
    dbp = str(tmp_path / "c.db"); out = str(tmp_path / "out")
    _mk_db_eid(dbp, [(7, "codex", "eid-ccc", 3000_000, 6)])
    with pytest.raises(ValueError):
        export.export_one(dbp, out, 7, external_id="eid-ccc")
    with pytest.raises(ValueError):
        export.export_one(dbp, out)


def test_parse_argv_external_id_forms():
    assert export.parse_argv(["--external-id", "eid-x", "outdir"]) == (None, "eid-x", ["outdir"], False)
    assert export.parse_argv(["--external-id=eid-y"]) == (None, "eid-y", [], False)
    assert export.parse_argv(["--conv", "7"]) == ("7", None, [], False)
    assert export.parse_argv(["out", "20"]) == (None, None, ["out", "20"], False)


def test_parse_argv_conv_and_eid_mutually_exclusive():
    with pytest.raises(ValueError):
        export.parse_argv(["--conv", "7", "--external-id", "eid-x"])
