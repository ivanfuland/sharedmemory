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
        (2, "tool_result", "orphan",   None, None),          # 无 id → 推导出 unpaired
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
    """坏 extra 不崩;结果侧标 unpaired = 失败安全:解不出 id 就无法核实配对,绝不断言配对。
    (render 输出不变 —— 无 id 的结果本来就走 is_res fallback 标 [unpaired]。)"""
    dbp = str(tmp_path / "badbin2.db")
    _mk_db(dbp, [(0, "tool_result", "x", None, b"\xc1\xff\xff")])
    m = reader.read_messages(dbp, 1)[0]
    assert m.tool_call_id is None and m.unpaired is True and m.content == "x"


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
    assert msgs[0].tool_call_id is None and msgs[0].unpaired is True   # 失败安全,同上
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


# ── unpaired 由 reader 在整会话视野内推导,不读 extra 字段 ──
# franken / CASS 从不写 `extra.unpaired`(实测源码 0 命中),照旧契约去读它 = 死代码。
# reader 一次读完整个会话,手握全部 tool_call 的 id,判"这条结果配不上任何调用"比
# franken 逐条 emit 时准。实测真库 66668 条 tool_result 中真孤儿 1 条(会话起点截断)。

def test_unpaired_derived_when_id_matches_no_call(tmp_path):
    """真库 conv 571 的形状:tool_result 在 idx=0,其 id 的调用发生在本会话记录开始之前。
    旧行为渲染成 [#toolu_x] —— 指向一个 transcript 里不存在的调用。"""
    dbp = str(tmp_path / "dangling.db")
    _mk_db(dbp, [
        (0, "tool_result", "Found 5 memories", None, _bin({"tool_call_id": "toolu_x"})),
        (1, "tool_call",   "read(...)",        None, _bin({"tool_call_id": "toolu_y"})),
        (2, "tool_result", "file body",        None, _bin({"tool_call_id": "toolu_y"})),
    ])
    msgs = reader.read_messages(dbp, 1)
    assert msgs[0].unpaired is True and msgs[0].tool_call_id == "toolu_x"   # id 保留,但标 unpaired
    assert msgs[2].unpaired is False and msgs[2].tool_call_id == "toolu_y"


def test_unpaired_derived_when_result_has_no_id(tmp_path):
    dbp = str(tmp_path / "noid.db")
    _mk_db(dbp, [(0, "tool_result", "x", None, None)])
    assert reader.read_messages(dbp, 1)[0].unpaired is True


def test_pairing_is_by_id_not_order(tmp_path):
    """结果先于调用出现(乱序)仍算配对:契约 P-原则-3 就是不靠顺序。"""
    dbp = str(tmp_path / "order.db")
    _mk_db(dbp, [
        (0, "tool_result", "out", None, _bin({"tool_call_id": "c1"})),
        (1, "tool_call",   "cmd", None, _bin({"tool_call_id": "c1"})),
    ])
    assert reader.read_messages(dbp, 1)[0].unpaired is False


def test_dangling_tool_call_never_marked_unpaired(tmp_path):
    """调用无结果(会话中途结束,真库 94 条)不是危险场景:不会被误配。只标结果侧。"""
    dbp = str(tmp_path / "dangle.db")
    _mk_db(dbp, [(0, "tool_call", "cmd", None, _bin({"tool_call_id": "c1"}))])
    m = reader.read_messages(dbp, 1)[0]
    assert m.unpaired is False and m.tool_call_id == "c1"


def test_extra_unpaired_field_is_ignored(tmp_path):
    """没人写这个字段;真写了也不能推翻推导(否则坏数据能伪造配对状态)。"""
    dbp = str(tmp_path / "ignore.db")
    _mk_db(dbp, [
        (0, "tool_call",   "cmd", None, _bin({"tool_call_id": "c1"})),
        (1, "tool_result", "out", None, _bin({"tool_call_id": "c1", "unpaired": True})),
        (2, "tool_result", "orp", None, _bin({"unpaired": False})),
    ])
    msgs = reader.read_messages(dbp, 1)
    assert msgs[1].unpaired is False     # 字段说 True,但 id 真配上了 → False
    assert msgs[2].unpaired is True      # 字段说 False,但无 id → True


def test_bad_call_extra_cascades_to_unpaired_result(tmp_path):
    """tool_call 的 extra 坏掉 → 它的 id 进不了 call_ids → 本来配得上的结果被标 unpaired。
    有意为之:级联方向是保守的(宁可标"配不上",不假装配上)。非意外行为,故钉死。"""
    dbp = str(tmp_path / "cascade.db")
    _mk_db(dbp, [
        (0, "tool_call",   "cmd", None, b"\xc1\xff\xff"),               # 坏 msgpack
        (1, "tool_result", "out", None, _bin({"tool_call_id": "c1"})),   # 好数据,但配不上了
    ])
    msgs = reader.read_messages(dbp, 1)
    assert msgs[0].tool_call_id is None and msgs[0].unpaired is False    # call 侧永不标
    assert msgs[1].tool_call_id == "c1" and msgs[1].unpaired is True


def test_legacy_result_roles_not_derived(tmp_path):
    """legacy tool / toolResult 全无配对信息,逐条标 unpaired 是噪声无信号。
    render 的 is_res fallback 已给它们标记,推导不插手。"""
    dbp = str(tmp_path / "legacy.db")
    _mk_db(dbp, [
        (0, "tool",       "out", None, None),
        (1, "toolResult", "out", None, None),
    ])
    msgs = reader.read_messages(dbp, 1)
    assert msgs[0].unpaired is False and msgs[1].unpaired is False


# ── 稳定会话身份:external_id / source_id 列 ──

def _mk_conv_db(path, with_id_cols):
    """建一个只有 conversations/agents/messages 的最小库。with_id_cols=False 模拟老/合成 schema。"""
    idcols = ", external_id TEXT, source_id TEXT" if with_id_cols else ""
    db = sqlite3.connect(path)
    db.executescript(f"""
        CREATE TABLE agents(id INTEGER PRIMARY KEY, slug TEXT);
        CREATE TABLE conversations(id INTEGER PRIMARY KEY, agent_id INTEGER, title TEXT,
            workspace_id INTEGER, source_path TEXT, started_at INTEGER,
            last_message_created_at INTEGER, primary_model TEXT{idcols});
        CREATE TABLE workspaces(id INTEGER PRIMARY KEY, path TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER,
            idx INTEGER, role TEXT, content TEXT, extra_json TEXT, extra_bin BLOB);
        INSERT INTO agents(id, slug) VALUES(1, 'codex');
    """)
    if with_id_cols:
        db.execute("INSERT INTO conversations(id,agent_id,title,source_path,started_at,"
                   "last_message_created_at,external_id,source_id) VALUES(1,1,'t','/p',1735660800000,"
                   "1735660800000,'.codex/sessions/x/rollout-abc','local')")
    else:
        db.execute("INSERT INTO conversations(id,agent_id,title,source_path,started_at,"
                   "last_message_created_at) VALUES(1,1,'t','/p',1735660800000,1735660800000)")
    db.execute("INSERT INTO messages(conversation_id,idx,role,content) VALUES(1,0,'user','hi')")
    db.commit(); db.close()


def test_reader_meta_carries_stable_id_cols(tmp_path):
    dbp = str(tmp_path / "withid.db"); _mk_conv_db(dbp, True)
    m = reader.get_conversation(dbp, 1)
    assert m["external_id"] == ".codex/sessions/x/rollout-abc" and m["source_id"] == "local"
    assert reader.select_conversations(dbp, min_turns=1)[0]["external_id"] == ".codex/sessions/x/rollout-abc"


def test_reader_tolerates_schema_without_id_cols(tmp_path):
    """老/合成 schema 缺 external_id/source_id → 不崩,meta 里也不伪造这两个键(codex plan R0 P0)。"""
    dbp = str(tmp_path / "noid.db"); _mk_conv_db(dbp, False)
    m = reader.get_conversation(dbp, 1)
    assert m is not None and "external_id" not in m and "source_id" not in m
    assert reader.select_conversations(dbp, min_turns=1)[0]["id"] == 1


# ── 类型收紧（codex 复审 P2）──


def test_non_string_tool_call_id_is_dropped(tmp_path):
    """bytes / int 的 id 会被 render 渲染成 `[#b'abc']`；契约是 Optional[str]，宁可当没有。"""
    dbp = str(tmp_path / "tid.db")
    _mk_db(dbp, [
        (0, "tool_call",   "x", None, _bin({"tool_call_id": b"abc"})),
        (1, "tool_call",   "y", None, _bin({"tool_call_id": 123})),
        (2, "tool_call",   "z", None, _bin({"tool_call_id": ""})),
        (3, "tool_call",   "w", None, _bin({"tool_call_id": "ok"})),
    ])
    msgs = reader.read_messages(dbp, 1)
    assert msgs[0].tool_call_id is None
    assert msgs[1].tool_call_id is None
    assert msgs[2].tool_call_id is None
    assert msgs[3].tool_call_id == "ok"


def test_memory_error_is_not_swallowed(tmp_path, monkeypatch):
    """系统性资源错误必须 loud fail，不能被"坏数据降级"路径吞掉。"""
    import pytest
    dbp = str(tmp_path / "mem.db")
    _mk_db(dbp, [(0, "tool_call", "x", None, _bin({"tool_call_id": "c1"}))])

    def boom(*a, **k):
        raise MemoryError("simulated")
    monkeypatch.setattr(reader.msgpack, "unpackb", boom)
    with pytest.raises(MemoryError):
        reader.read_messages(dbp, 1)
