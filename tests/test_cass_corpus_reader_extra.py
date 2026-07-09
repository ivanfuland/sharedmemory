import json
import sqlite3

import msgpack

from cass_corpus import reader


# ⚠️ fixture 必须能写 extra_bin。CASS 真库把非空 extra 存进 extra_bin(msgpack)、extra_json=NULL;
# 早期 fixture 只写 extra_json,与真实存储路径相反 → "reader 只读 extra_json" 的 bug 全绿通过测试,
# 而在真数据上 [#id] 配对标记 100% 静默 no-op(实测真库命中 0 条)。见 D3。
def _mk_db(path, rows):
    """rows = [(idx, role, content, extra_json, extra_bin)]"""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE conversations(id INTEGER PRIMARY KEY, agent_id INTEGER,
            title TEXT, workspace_id INTEGER, source_path TEXT,
            started_at INTEGER, last_message_created_at INTEGER, primary_model TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER,
            idx INTEGER, role TEXT, author TEXT, created_at INTEGER,
            content TEXT, extra_json TEXT, extra_bin BLOB);
    """)
    for idx, role, content, extra_json, extra_bin in rows:
        db.execute(
            "INSERT INTO messages(conversation_id, idx, role, content, extra_json, extra_bin)"
            " VALUES(1,?,?,?,?,?)",
            (idx, role, content, extra_json, extra_bin))
    db.commit(); db.close()


def _bin(obj):
    return msgpack.packb(obj, use_bin_type=True)


# ── 真实存储路径:extra_bin(msgpack),extra_json=NULL ──

def test_reader_parses_pairing_from_extra_bin(tmp_path):
    """真库形状:非空 extra 全在 extra_bin。这是 D3 修复前 100% 漏掉的路径。"""
    dbp = str(tmp_path / "bin.db")
    _mk_db(dbp, [
        (0, "tool_call",   "Bash: ls", None, _bin({"tool_call_id": "toolu_1", "tool_call_args": {"cmd": "ls"}})),
        (1, "tool_result", "OK",       None, _bin({"tool_call_id": "toolu_1"})),
        (2, "tool_result", "orphan",   None, _bin({"unpaired": True})),
        (3, "user",        "hi",       None, None),
    ])
    msgs = reader.read_messages(dbp, 1)
    assert msgs[0].tool_call_id == "toolu_1"
    assert msgs[1].tool_call_id == "toolu_1" and msgs[1].unpaired is False
    assert msgs[2].unpaired is True and msgs[2].tool_call_id is None
    assert msgs[3].tool_call_id is None and msgs[3].unpaired is False


def test_extra_bin_wins_over_extra_json(tmp_path):
    """存储契约上二者互斥;真出现双写时以 extra_bin 为准(它是新写路径)。"""
    dbp = str(tmp_path / "both.db")
    _mk_db(dbp, [(0, "tool_call", "x", json.dumps({"tool_call_id": "stale"}), _bin({"tool_call_id": "fresh"}))])
    assert reader.read_messages(dbp, 1)[0].tool_call_id == "fresh"


def test_bad_extra_bin_falls_back_to_extra_json(tmp_path):
    dbp = str(tmp_path / "badbin.db")
    _mk_db(dbp, [(0, "tool_result", "x", json.dumps({"tool_call_id": "c1"}), b"\xc1not-msgpack")])
    assert reader.read_messages(dbp, 1)[0].tool_call_id == "c1"


def test_bad_extra_bin_without_json_degrades(tmp_path):
    dbp = str(tmp_path / "badbin2.db")
    _mk_db(dbp, [(0, "tool_result", "x", None, b"\xc1\xff\xff")])
    m = reader.read_messages(dbp, 1)[0]
    assert m.tool_call_id is None and m.unpaired is False and m.content == "x"


def test_non_dict_extra_bin_degrades(tmp_path):
    """msgpack 合法但不是 map(数组/标量)→ 降级,不崩。"""
    dbp = str(tmp_path / "scalar.db")
    _mk_db(dbp, [(0, "tool_call", "x", None, _bin([1, 2, 3]))])
    assert reader.read_messages(dbp, 1)[0].tool_call_id is None


# ── 兼容路径:extra_json(空对象 / 历史 raw 包装) ──

def test_reader_parses_tool_call_id_and_unpaired_from_extra_json(tmp_path):
    dbp = str(tmp_path / "t.db")
    _mk_db(dbp, [
        (0, "tool_call",   "Bash: ls", json.dumps({"tool_call_id": "c1"}), None),
        (1, "tool_result", "OK",       json.dumps({"tool_call_id": "c1"}), None),
        (2, "tool_result", "orphan",   json.dumps({"unpaired": True}),     None),
        (3, "user",        "hi",       None,                               None),
    ])
    msgs = reader.read_messages(dbp, 1)
    assert msgs[0].tool_call_id == "c1"
    assert msgs[1].tool_call_id == "c1" and msgs[1].unpaired is False
    assert msgs[2].unpaired is True and msgs[2].tool_call_id is None
    assert msgs[3].tool_call_id is None and msgs[3].unpaired is False


def test_reader_tolerates_bad_extra_json(tmp_path):
    dbp = str(tmp_path / "t.db")
    _mk_db(dbp, [(0, "tool_result", "x", "{not valid json", None), (1, "user", "hi", "", None)])
    msgs = reader.read_messages(dbp, 1)                      # 坏 JSON 不崩,降级为无配对信息
    assert msgs[0].tool_call_id is None and msgs[0].unpaired is False
    assert msgs[1].content == "hi"


def test_empty_object_extra_json_degrades(tmp_path):
    """CASS 对空 extra 写字面 '{}' 到 extra_json(D1 那 4 个大会话就是这形状)。"""
    dbp = str(tmp_path / "empty.db")
    _mk_db(dbp, [(0, "tool_call", "x", "{}", None)])
    m = reader.read_messages(dbp, 1)[0]
    assert m.tool_call_id is None and m.unpaired is False


# ── 降级路径:extra 列不存在 ──

def test_reader_without_extra_columns(tmp_path):
    # 老/合成 schema 一个 extra 列都没有 → PRAGMA 降级为 base SQL,不崩(codex plan R0 P0)
    dbp = str(tmp_path / "noextra.db")
    db = sqlite3.connect(dbp)
    db.executescript(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER, idx INTEGER,"
        " role TEXT, content TEXT);")
    db.execute("INSERT INTO messages(conversation_id, idx, role, content) VALUES(1,0,'user','hi')")
    db.commit(); db.close()
    msgs = reader.read_messages(dbp, 1)
    assert msgs[0].content == "hi" and msgs[0].tool_call_id is None and msgs[0].unpaired is False


def test_reader_with_only_extra_json_column(tmp_path):
    """只有 extra_json 列(无 extra_bin)→ 仍能解析,不因缺列报错。"""
    dbp = str(tmp_path / "jsononly.db")
    db = sqlite3.connect(dbp)
    db.executescript(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER, idx INTEGER,"
        " role TEXT, content TEXT, extra_json TEXT);")
    db.execute("INSERT INTO messages(conversation_id, idx, role, content, extra_json)"
               " VALUES(1,0,'tool_call','x',?)", (json.dumps({"tool_call_id": "c9"}),))
    db.commit(); db.close()
    assert reader.read_messages(dbp, 1)[0].tool_call_id == "c9"


def test_reader_with_only_extra_bin_column(tmp_path):
    dbp = str(tmp_path / "binonly.db")
    db = sqlite3.connect(dbp)
    db.executescript(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER, idx INTEGER,"
        " role TEXT, content TEXT, extra_bin BLOB);")
    db.execute("INSERT INTO messages(conversation_id, idx, role, content, extra_bin)"
               " VALUES(1,0,'tool_call','x',?)", (_bin({"tool_call_id": "c8"}),))
    db.commit(); db.close()
    assert reader.read_messages(dbp, 1)[0].tool_call_id == "c8"
