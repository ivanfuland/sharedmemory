"""`infra/cass-semantic/structure-probe.sh` 单测(跑真脚本 + 手造 sqlite fixture)。

签名 A(gap)可用健康 sqlite 合成(插行后删中间行);签名 B(seek-invisible)是真实
b-tree 损坏,无法用合法 SQL 制造——已用 2026-07-16 事故的坏库副本做过双向已知答案
验证(活库 clean / 坏库同时命中 gap + seek-invisible),见引入 commit 记录。
"""
from __future__ import annotations

import os
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


# ---------------------------------------------------------------------------
# 签名 B(seek-invisible)自动化正例(codex R2-F4):对手造多页 b-tree 的根页分隔键
# 做 ±1 字节手术,复刻 2026-07-16 生产损坏的「陈旧分隔键」形态——scan 可见、seek 不可达。
# ---------------------------------------------------------------------------

def _varint(page: bytes, off: int) -> tuple[int, int]:
    v = 0
    for i in range(9):
        b = page[off + i]
        if i == 8:
            return (v << 8) | b, 9
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, i + 1
    raise AssertionError("unreachable")


def _corrupt_root_separator(db: pathlib.Path) -> int:
    """把 conversations 根页 cell0 分隔键 ±1(保编码长度),返回被害 rowid。"""
    con = sqlite3.connect(db)
    (root,) = con.execute("SELECT rootpage FROM sqlite_master WHERE name='conversations'").fetchone()
    (ps,) = con.execute("PRAGMA page_size").fetchone()
    con.close()
    data = bytearray(db.read_bytes())
    base = (root - 1) * ps
    assert data[base] == 0x05, "fixture 未形成 interior 根页,加大行数"
    cp = int.from_bytes(data[base + 12 : base + 14], "big")
    sep, ln = _varint(bytes(data[base + cp + 4 : base + cp + 13]), 0)
    last = base + cp + 4 + ln - 1
    low7 = data[last] & 0x7F
    # 低 7 位 -1(>0 时)或 +1:值变 ±1 而编码长度不变
    data[last] = (data[last] & 0x80) | (low7 - 1 if low7 > 0 else low7 + 1)
    db.write_bytes(bytes(data))
    return sep  # 分隔键变小时,原 sep 行 seek 会被误导向右子树 → 不可达


def _mk_multipage_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "multi.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, external_id TEXT)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER, content TEXT)")
    con.executemany(
        "INSERT INTO conversations (id, external_id) VALUES (?, ?)",
        [(i, f"conv-{i:06d}-{'x' * 40}") for i in range(1, 2001)],
    )
    con.execute("INSERT INTO messages (id, conversation_id, content) VALUES (1, 1, 'x')")
    con.commit()
    con.close()
    return db


def test_seek_invisible_signature_detected(tmp_path: pathlib.Path) -> None:
    db = _mk_multipage_db(tmp_path)
    r0 = _run(db)
    assert r0.returncode == 0, f"损坏前应 clean:\n{r0.stdout}{r0.stderr}"
    victim = _corrupt_root_separator(db)
    r = _run(db)
    assert r.returncode == 1, f"分隔键手术后应检出(victim sep={victim}):\n{r.stdout}{r.stderr}"
    assert "signature=seek-invisible" in r.stdout
    assert "table=conversations" in r.stdout


def test_probe_query_timeout_fails_loud(tmp_path: pathlib.Path) -> None:
    """codex R2-F1/R3:查询超时 → GNU timeout 契约 rc=124 原样传播(set -e),绝不静默当 clean。
    外层 150s timeout 与 --kill-after 属 GNU timeout 标准语义,自动化不真等 150s,不另测。"""
    db = _mk_multipage_db(tmp_path)
    env = {**os.environ, "STRUCTURE_PROBE_QUERY_TIMEOUT": "0.001"}
    r = subprocess.run(["bash", str(PROBE), str(db)], capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 124, f"应为 GNU timeout 契约 rc=124,实得 {r.returncode}:\n{r.stdout}{r.stderr}"
    assert "clean" not in r.stdout


# 轮转 NUL 安全(codex R3-P1)在 test_index_pull_probe_wiring.py 里对真脚本+含空格目录验证,
# 此处不复制脚本片段(片段复制会随脚本演进漂移成假绿)。
