"""infra/backup/cass/cass_backup_gate.py 的单元测试（腿 2：scan-vs-seek + EQP 自证）。

覆盖 Task 4 brief 的全部测试要点：
  - 健康 synth_dd 全表 scan-vs-seek 合计 0 → PASS。
  - V5e：`LEG2_SQL` 被 monkeypatch 成去掉 `NOT INDEXED` 的劣化版 → EQP 自证必须 FAIL
    （必须是「EQP 不符」这个原因 FAIL，不是「计数非 0」）。
  - 虚表 / `WITHOUT ROWID` 表被跳过：`_leg2_rowid_tables` 直接断言选表集合。
  - EQP 自证的三个子条件（`SCAN a` / `SEARCH b USING INTEGER PRIMARY KEY` /
    不含 `COVERING INDEX`）各自独立可红——用 `_leg2_eqp_self_certifies` 纯函数
    喂手造的 EQP 行，逐条验证「缺一个条件就必须 False」。
  - 受控 FAIL：任何表的 EQP 或主查询抛 `sqlite3.DatabaseError`（真实损坏抛的是
    这个父类，`except OperationalError` 接不住）→ `LegResult(ok=False)` 且 detail
    带表名，不裸 crash。两个分支（EQP 就 raise / EQP 成功但主查询 raise）分别覆盖。
    腿 2 **无任何表级豁免**——`NOT INDEXED` + rowid seek 不经过二级索引，实测在
    生产坏 DDL 的 `fts_messages_config` 上正常返回 0（spec §5.3 无豁免条款）。
"""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from cass_backup_gate import (
    LEG2_SQL,
    LegResult,
    _leg2_eqp_self_certifies,
    _leg2_rowid_tables,
    leg2,
)

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd 模板"
)


# ---------------------------------------------------------------------------
# leg2 端到端：健康 synth_dd 全表合计 0 → PASS
# ---------------------------------------------------------------------------


@requires_cass
def test_leg2_pass_on_synth_dd(synth_dd):
    con = sqlite3.connect(str(synth_dd / "agent_search.db"))
    try:
        result = leg2(con)
    finally:
        con.close()
    assert isinstance(result, LegResult)
    assert result.ok is True


# ---------------------------------------------------------------------------
# V5e：去掉 NOT INDEXED 的劣化 SQL → EQP 自证必须 FAIL（不是计数非 0）
# ---------------------------------------------------------------------------

_DEGRADED_SQL_NO_NOT_INDEXED = (
    'SELECT COUNT(*) FROM "{table}" AS a '
    'WHERE NOT EXISTS (SELECT 1 FROM "{table}" AS b WHERE b.rowid = a.rowid)'
)


@requires_cass
def test_leg2_degraded_sql_without_not_indexed_fails_eqp_self_check(synth_dd, monkeypatch):
    import cass_backup_gate

    monkeypatch.setattr(cass_backup_gate, "LEG2_SQL", _DEGRADED_SQL_NO_NOT_INDEXED)

    con = sqlite3.connect(str(synth_dd / "agent_search.db"))
    try:
        result = cass_backup_gate.leg2(con)
    finally:
        con.close()

    assert result.ok is False
    # 必须是 EQP 自证失败（能看到 COVERING INDEX 这个诊断线索），不是「分歧计数非 0」
    # 这种误报——去掉 NOT INDEXED 后表内容本身没变，计数仍应是 0。
    assert "COVERING INDEX" in result.detail
    assert "分歧=" not in result.detail


# ---------------------------------------------------------------------------
# 虚表 / WITHOUT ROWID 表被跳过：直接断言 _leg2_rowid_tables 的选表集合
# ---------------------------------------------------------------------------


def _build_fts5_probe_db(path) -> sqlite3.Connection:
    """构造与 spec §2.6 一致的 FTS5 影子表形状：`fts_messages`（虚表）、
    `fts_messages_config` / `fts_messages_idx`（WITHOUT ROWID）、
    `fts_messages_content` / `fts_messages_data` / `fts_messages_docsize`（普通 rowid 表），
    外加两张业务表 `conversations`/`messages` 佐证「该查的还在查」。"""
    con = sqlite3.connect(str(path))
    con.execute("CREATE VIRTUAL TABLE fts_messages USING fts5(content)")
    con.execute("INSERT INTO fts_messages(content) VALUES ('hello world')")
    con.execute("CREATE TABLE conversations(id INTEGER PRIMARY KEY, title TEXT)")
    con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT)")
    con.commit()
    return con


def test_leg2_rowid_tables_skips_virtual_and_without_rowid(tmp_path):
    con = _build_fts5_probe_db(tmp_path / "probe.db")
    try:
        tables = set(_leg2_rowid_tables(con))
    finally:
        con.close()

    # 虚表本身必须不参与
    assert "fts_messages" not in tables
    # WITHOUT ROWID 的两张影子表必须不参与
    assert "fts_messages_config" not in tables
    assert "fts_messages_idx" not in tables
    # 普通 rowid 影子表必须参与（它们是「隐藏在虚表背后」的真表，不能被连坐跳过）
    assert "fts_messages_content" in tables
    assert "fts_messages_data" in tables
    assert "fts_messages_docsize" in tables
    # 业务表必须参与
    assert "conversations" in tables
    assert "messages" in tables


def test_leg2_end_to_end_on_fts5_probe_db_passes(tmp_path):
    """确认虚表场景下 leg2 完整跑一遍也是 PASS（选表 + EQP 自证 + 计数都得联动对）。"""
    con = _build_fts5_probe_db(tmp_path / "probe2.db")
    try:
        result = leg2(con)
    finally:
        con.close()
    assert result.ok is True


# ---------------------------------------------------------------------------
# EQP 自证三个子条件各自独立可红（纯函数，手造 EQP 行，避免依赖脆弱的真实查询计划）
# ---------------------------------------------------------------------------


def test_eqp_self_certifies_true_when_all_three_conditions_met():
    text = "SCAN a | SEARCH b USING INTEGER PRIMARY KEY (rowid=?)"
    assert _leg2_eqp_self_certifies(text) is True


def test_eqp_self_certifies_false_missing_scan_a():
    text = "SEARCH b USING INTEGER PRIMARY KEY (rowid=?)"
    assert _leg2_eqp_self_certifies(text) is False


def test_eqp_self_certifies_false_missing_search_b():
    text = "SCAN a"
    assert _leg2_eqp_self_certifies(text) is False


def test_eqp_self_certifies_false_when_covering_index_present():
    # 即使 SCAN a / SEARCH b 两个子串都在，只要出现 COVERING INDEX 就必须 False。
    text = (
        "SCAN a USING COVERING INDEX idx_conversations_agent_started | "
        "SEARCH b USING INTEGER PRIMARY KEY (rowid=?)"
    )
    assert _leg2_eqp_self_certifies(text) is False


# ---------------------------------------------------------------------------
# 受控 FAIL：EQP 或主查询抛 sqlite3.DatabaseError → LegResult(ok=False)，不裸 crash。
# 真实损坏抛的是 DatabaseError 这个父类（reviewer 本机复现 `except OperationalError`
# 接不住），proxy 必须 raise DatabaseError 才测到位。腿 2 无任何表级豁免。
# ---------------------------------------------------------------------------


class _RaisingTableConnection:
    """包一层真连接：对指定表名的查询按阶段抛 `sqlite3.DatabaseError`（真实损坏的
    异常类），其余查询原样代理给真连接。`stage` 区分两个失败分支：
      - "eqp"：EQP 调用（SQL 以 `EXPLAIN QUERY PLAN` 开头）就 raise；
      - "main"：EQP 放行、只在主查询（非 EXPLAIN 前缀）时 raise——这是真实损坏
        的实际形态（查询计划编译得出来，执行到坏页才炸）。"""

    def __init__(self, real_con, table: str, stage: str):
        assert stage in ("eqp", "main")
        self._real = real_con
        self._table = table
        self._stage = stage

    def execute(self, sql, *args, **kwargs):
        if f'"{self._table}"' in sql and "sqlite_master" not in sql:
            is_eqp = sql.lstrip().startswith("EXPLAIN QUERY PLAN")
            if (self._stage == "eqp" and is_eqp) or (self._stage == "main" and not is_eqp):
                raise sqlite3.DatabaseError("database disk image is malformed")
        return self._real.execute(sql, *args, **kwargs)


def _build_probe_db_two_tables():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE conversations(id INTEGER PRIMARY KEY, title TEXT)")
    con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT)")
    con.commit()
    return con


def test_leg2_controlled_fail_when_main_query_raises_database_error():
    """(a) EQP 成功、主查询 raise（真实损坏的实际形态）→ 受控 FAIL 且 detail 带表名。"""
    con = _build_probe_db_two_tables()
    proxy = _RaisingTableConnection(con, "conversations", stage="main")
    try:
        result = leg2(proxy)
    finally:
        con.close()
    assert result.ok is False
    assert "conversations" in result.detail


def test_leg2_controlled_fail_when_eqp_raises_database_error():
    """(b) EQP 阶段就 raise → 受控 FAIL 且 detail 带表名。"""
    con = _build_probe_db_two_tables()
    proxy = _RaisingTableConnection(con, "messages", stage="eqp")
    try:
        result = leg2(proxy)
    finally:
        con.close()
    assert result.ok is False
    assert "messages" in result.detail
