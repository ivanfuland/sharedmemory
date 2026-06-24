import sqlite3, json, pytest
from distill import state, cass_reader, config

def _canon(tmp_path):
    """造一个最小 CASS canonical 规范化库（messages/conversations/agents/workspaces）。"""
    p = str(tmp_path / "agent_search.db")
    db = sqlite3.connect(p)
    db.executescript("""
      CREATE TABLE agents(id INTEGER PRIMARY KEY, slug TEXT, name TEXT, kind TEXT);
      CREATE TABLE workspaces(id INTEGER PRIMARY KEY, path TEXT, display_name TEXT);
      CREATE TABLE conversations(id INTEGER PRIMARY KEY, agent_id INT, workspace_id INT, source_path TEXT, external_id TEXT, title TEXT);
      CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INT, idx INT, role TEXT, author TEXT, created_at INT, content TEXT, extra_json TEXT, extra_bin BLOB);
      INSERT INTO agents VALUES (1,'claude_code','CC','cli');
      INSERT INTO workspaces VALUES (1,'/home/ivan/projects/foo','foo');
      INSERT INTO conversations VALUES (1,1,1,'/p/sess.jsonl','ext1','t');
      INSERT INTO messages VALUES (1,1,0,'user','ivan',1700000000000,'我们决定用 X','{}',NULL);
      INSERT INTO messages VALUES (2,1,1,'assistant','cc',1700000001000,'好的','{}',NULL);
    """)
    db.commit(); db.close()
    return p

def test_fingerprint_mismatch_refuses(tmp_path):
    canon = _canon(tmp_path)
    fp = str(tmp_path / "fp"); open(fp, "w").write("deadbeef-not-matching\n")
    with pytest.raises(cass_reader.FingerprintMismatch):
        cass_reader.verify_fingerprint(canon, fp)

def test_read_advances_cursor_and_writes_raw_same_txn(tmp_path):
    canon = _canon(tmp_path)
    c = state.connect(str(tmp_path / "s.db"))
    n = cass_reader.read_spans(canon, c, source_id="ubuntu-cc", agent_slug="claude_code", max_messages=100)
    assert n == 1  # 1 conversation → 1 raw_work_item
    assert c.execute("SELECT stream_position FROM cursor WHERE source_id='ubuntu-cc'").fetchone()[0] == 2
    row = c.execute("SELECT span_start,span_end,session_ref,status FROM raw_work_item").fetchone()
    assert (row["span_start"], row["span_end"], row["status"]) == (1, 2, "new")

def test_reread_is_idempotent_noop(tmp_path):
    canon = _canon(tmp_path)
    c = state.connect(str(tmp_path / "s.db"))
    cass_reader.read_spans(canon, c, "ubuntu-cc", "claude_code", 100)
    # 游标已到 2，再读应 0 新增（无 id>2 的消息）
    assert cass_reader.read_spans(canon, c, "ubuntu-cc", "claude_code", 100) == 0
    assert c.execute("SELECT COUNT(*) FROM raw_work_item").fetchone()[0] == 1
