# tests/test_export_conv.py
# export --conv <id> 单条精确导出（Inngest F3 逐条驱动用）。TDD：先失败。
# 全合成数据（PUBLIC 仓隐私）。messages 带 created_at（毫秒 epoch）以验 max_message_ts / exported_ts。
import os
import sqlite3
from cass_corpus import export, render


def _mk_db(path, convs):
    """convs: list of (id, agent_slug, last_ts, n_msgs, msg_len)。
    messages.created_at = last_ts - (n-1-i)*1000（i 越大越新），max = last_ts。"""
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE agents(id INTEGER PRIMARY KEY, slug TEXT);"
        "CREATE TABLE workspaces(id INTEGER PRIMARY KEY, path TEXT);"
        "CREATE TABLE conversations(id INTEGER PRIMARY KEY, title TEXT, workspace_id INT,"
        " source_path TEXT, started_at INT, last_message_created_at INT, agent_id INT, primary_model TEXT);"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INT, idx INT, role TEXT,"
        " content TEXT, created_at INT);"
    )
    mid = 1
    for cid, slug, last_ts, n, msg_len in convs:
        db.execute("INSERT OR IGNORE INTO agents VALUES(?,?)", (cid + 100, slug))
        db.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?)",
            (cid, f"t{cid}", None, None, last_ts, last_ts, cid + 100, "m"),
        )
        for i in range(n):
            body = f"turn {i} " + ("lorem ipsum dolor sit amet " * msg_len)
            created = last_ts - (n - 1 - i) * 1000  # 最后一条 = last_ts
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", (mid, cid, i, "user", body, created))
            mid += 1
    db.commit()
    db.close()


def test_export_one_writes_file_and_reports(tmp_path):
    p = str(tmp_path / "c.db")
    out = str(tmp_path / "out")
    _mk_db(p, [(1898, "claude_code", 1782896385426, 6, 120)])  # 长会话，稳过 min_chars
    rep = export.export_one(p, out, 1898)
    meta = {"id": 1898, "agent": "claude_code", "title": "t1898",
            "started_at": 1782896385426}
    fn = render.transcript_filename(meta)
    target = os.path.join(out, fn)
    assert os.path.exists(target), f"transcript not written: {target}"
    assert os.path.getsize(target) > 0
    assert rep["total"] == 1
    assert rep["skipped"] == [] and rep["errors"] == []
    assert len(rep["written"]) == 1 and rep["written"][0][0] == fn
    assert rep["exported_ts"] == 1782896385426  # = max(messages.created_at)


def test_export_one_missing_conv_returns_total_0(tmp_path):
    p = str(tmp_path / "c.db")
    out = str(tmp_path / "out")
    _mk_db(p, [(1898, "claude_code", 1782896385426, 6, 120)])
    rep = export.export_one(p, out, 999999)  # 不存在
    assert rep["total"] == 0
    assert rep["written"] == [] and rep["skipped"] == [] and rep["errors"] == []


def test_export_one_too_short_is_skipped(tmp_path):
    p = str(tmp_path / "c.db")
    out = str(tmp_path / "out")
    _mk_db(p, [(42, "claude_code", 1782896385426, 2, 1)])  # 极短 → 渲染 < min_chars
    rep = export.export_one(p, out, 42)
    assert rep["total"] == 1
    assert rep["written"] == []
    assert len(rep["skipped"]) == 1
    assert rep["exported_ts"] == 1782896385426  # 即便 skip 也记内容版本
