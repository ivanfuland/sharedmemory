import json
import sqlite3
from cass_corpus import reader


def _mk_db(path, rows):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE conversations(id INTEGER PRIMARY KEY, agent_id INTEGER,
            title TEXT, workspace_id INTEGER, source_path TEXT,
            started_at INTEGER, last_message_created_at INTEGER, primary_model TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER,
            idx INTEGER, role TEXT, author TEXT, created_at INTEGER,
            content TEXT, extra_json TEXT, extra_bin BLOB);
    """)
    for idx, role, content, extra in rows:
        db.execute("INSERT INTO messages(conversation_id, idx, role, content, extra_json) VALUES(1,?,?,?,?)",
                   (idx, role, content, extra))
    db.commit(); db.close()


def test_reader_parses_tool_call_id_and_unpaired(tmp_path):
    dbp = str(tmp_path / "t.db")
    _mk_db(dbp, [
        (0, "tool_call",   "Bash: ls", json.dumps({"tool_call_id": "c1"})),
        (1, "tool_result", "OK",       json.dumps({"tool_call_id": "c1"})),
        (2, "tool_result", "orphan",   json.dumps({"unpaired": True})),
        (3, "user",        "hi",       None),
    ])
    msgs = reader.read_messages(dbp, 1)
    assert msgs[0].tool_call_id == "c1"
    assert msgs[1].tool_call_id == "c1" and msgs[1].unpaired is False
    assert msgs[2].unpaired is True and msgs[2].tool_call_id is None
    assert msgs[3].tool_call_id is None and msgs[3].unpaired is False


def test_reader_tolerates_bad_extra_json(tmp_path):
    dbp = str(tmp_path / "t.db")
    _mk_db(dbp, [(0, "tool_result", "x", "{not valid json"), (1, "user", "hi", "")])
    msgs = reader.read_messages(dbp, 1)                      # 坏 JSON 不崩,降级为无配对信息
    assert msgs[0].tool_call_id is None and msgs[0].unpaired is False
    assert msgs[1].content == "hi"


def test_reader_without_extra_json_column(tmp_path):
    # 老/合成 schema 无 extra_json 列 → PRAGMA 降级为 base SQL,不崩(codex plan R0 P0)
    dbp = str(tmp_path / "noextra.db")
    db = sqlite3.connect(dbp)
    db.executescript(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER, idx INTEGER, role TEXT, content TEXT);")
    db.execute("INSERT INTO messages(conversation_id, idx, role, content) VALUES(1,0,'user','hi')")
    db.commit(); db.close()
    msgs = reader.read_messages(dbp, 1)
    assert msgs[0].content == "hi" and msgs[0].tool_call_id is None and msgs[0].unpaired is False
