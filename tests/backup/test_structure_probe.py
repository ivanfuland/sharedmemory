"""`infra/cass-semantic/structure-probe.sh` 单测(跑真脚本 + 手造 sqlite fixture)。

签名 A(gap)可用健康 sqlite 合成(插行后删中间行);签名 B(seek-invisible)是真实
b-tree 损坏,无法用合法 SQL 制造——已用 2026-07-16 事故的坏库副本做过双向已知答案
验证(活库 clean / 坏库同时命中 gap + seek-invisible),见引入 commit 记录。
"""
from __future__ import annotations

import pathlib
import sqlite3
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
PROBE = REPO / "infra" / "cass-semantic" / "structure-probe.sh"


def _mk_db(tmp_path: pathlib.Path, message_ids: list[int]) -> pathlib.Path:
    db = tmp_path / "probe.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, external_id TEXT)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER, content TEXT)")
    con.execute("INSERT INTO conversations (id, external_id) VALUES (1, 'c1')")
    for mid in message_ids:
        con.execute("INSERT INTO messages (id, conversation_id, content) VALUES (?, 1, 'x')", (mid,))
    con.commit()
    con.close()
    return db


def _run(db: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(PROBE), str(db)], capture_output=True, text=True, timeout=30)


def test_clean_db_passes(tmp_path: pathlib.Path) -> None:
    r = _run(_mk_db(tmp_path, [1, 2, 3]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "clean" in r.stdout


def test_gap_signature_detected(tmp_path: pathlib.Path) -> None:
    """id 空洞(COUNT != MAX)= 签名 A,必须 exit 1 且指名道姓。"""
    db = _mk_db(tmp_path, [1, 2, 3, 4])
    con = sqlite3.connect(db)
    con.execute("DELETE FROM messages WHERE id = 3")
    con.commit()
    con.close()
    r = _run(db)
    assert r.returncode == 1
    assert "signature=gap" in r.stdout
    assert "table=messages" in r.stdout


def test_missing_db_fails_loud(tmp_path: pathlib.Path) -> None:
    r = _run(tmp_path / "nope.db")
    assert r.returncode != 0
