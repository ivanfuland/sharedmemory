"""CASS canonical 读端契约测试（规范化 schema，JOIN-based read）。
环境变量 CASS_CANON_DB 指向 canonical sqlite（缺失则 skip——读端路线未启用）。
字段/读 SQL 来自 contracts/cass-canonical-fields.json。"""
import json
import os
import sqlite3
import subprocess
import pathlib
import pytest

CANON_DB = os.environ.get("CASS_CANON_DB")
REPO = pathlib.Path(__file__).resolve().parent.parent
FINGERPRINT_FILE = REPO / "contracts" / "cass-canonical.fingerprint"
FIELDS_FILE = REPO / "contracts" / "cass-canonical-fields.json"

pytestmark = pytest.mark.skipif(
    not CANON_DB or not pathlib.Path(CANON_DB).exists(),
    reason="CASS_CANON_DB 未设置或文件不存在（canonical 路线未启用）",
)


def _fields():
    return json.loads(FIELDS_FILE.read_text())


def _ro():
    return sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True)


def _current_fingerprint():
    script = REPO / "scripts" / "cass-schema-fingerprint.sh"
    out = subprocess.run(["bash", str(script), CANON_DB],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_fingerprint_matches_locked_baseline():
    """schema 指纹与锁定基线一致——漂移即 CASS 升级，需人工更新契约。"""
    assert _current_fingerprint() == FINGERPRINT_FILE.read_text().strip(), (
        "canonical schema 指纹漂移：CASS 升级，重跑 Task 3 更新契约后再放行蒸馏桥"
    )


def test_standard_sqlite3_can_read():
    con = _ro()
    tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    con.close()
    assert len(tables) > 0, "未读到任何表"


def test_read_sql_required_fields_present_and_nonempty():
    """JOIN read_sql 的每个 required 别名都能取出非空值（首条记录验完整性）。"""
    f = _fields()
    con = _ro()
    try:
        cur = con.execute(f"{f['read_sql']} ORDER BY cursor ASC LIMIT 1")
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
    finally:
        con.close()
    assert row is not None, "read_sql 读不出任何记录"
    rec = dict(zip(cols, row))
    for alias in f["required_aliases"]:
        assert alias in rec, f"read_sql 缺别名 {alias}"
        assert rec[alias] not in (None, ""), f"必需字段 {alias} 首条为空"


def test_cursor_is_integer_pk_unique_nonnull():
    """游标 messages.id 是 INTEGER PRIMARY KEY（rowid 别名，单调自增）+ 唯一 + 无 NULL。
    （ORDER BY+sorted 是恒真断言，故查列类型 + 计数，不查排序）"""
    f = _fields()
    col, table = f["cursor_col"], f["msg_table"]
    con = _ro()
    try:
        info = con.execute(f"PRAGMA table_info({table})").fetchall()
        colrow = next((r for r in info if r[1] == col), None)
        assert colrow is not None, f"游标列 {col} 不存在"
        coltype, pk = (colrow[2] or "").upper(), colrow[5]
        assert coltype == "INTEGER" and pk and pk > 0, (
            f"游标列 {col}: type={coltype!r} pk={pk}，非 INTEGER PRIMARY KEY"
        )
        total, non_null, distinct = con.execute(
            f"SELECT COUNT(*), COUNT({col}), COUNT(DISTINCT {col}) FROM {table}"
        ).fetchone()
    finally:
        con.close()
    assert total > 0, "消息表为空——先 cass index"
    assert total == non_null, f"游标列 {col} 有 NULL（{total-non_null} 行）"
    assert non_null == distinct, f"游标列 {col} 有重复"
