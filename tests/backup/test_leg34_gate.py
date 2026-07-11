"""infra/backup/cass/cass_backup_gate.py 的单元测试（腿 3：schema 与行数普查 + rebaseline，
spec §5.4 / §5.7）。

腿 4 的测试属于后续 task，本文件当前只覆盖腿 3。

覆盖 Task 5 brief 的测试要点：
  - 首晚 `prev_census`/`prev_fingerprint` 均为 None → 登记模式，ok=True，census/fingerprint
    非空。
  - 健康 synth_dd 以自身为基线（第二次跑）→ PASS。
  - `fts_messages_config` 无条件 EXEMPT——即使健康合成库上它其实能 COUNT 成功。
  - 攻击①（删 `meta` 的 schema 条目）→ 必需清单报 `meta` 缺失 **且** 普查报表里 `meta`
    消失（V4）；rebaseline=True 时该攻击仍 FAIL（必需清单不受 rebaseline 影响）。
  - 攻击③（清空 `agents`）→ 严格不减 FAIL：prev 值 > 0、cur=0（V5a）。
  - `sources` 2→1（直接删 synth_dd 的一行 `sources`）→ FAIL（V5d5「丢一半」形态）。
  - 新增表 → PASS（只增不减合法）：直接测 `_leg3_compare_census` 纯函数，避免与 schema
    指纹比对（新增表必然改变指纹）互相干扰。
  - `ALTER TABLE agents ADD COLUMN x` → schema 指纹 FAIL 且报文含「重设基线」；
    `rebaseline=True` 时同一变更改为 PASS。
  - READ_FAILED 哨兵双向 fail-closed（review 修复）：非豁免表 COUNT raise →
    当晚 FAIL 且 census 记 `"READ_FAILED"`（字符串哨兵，绝不用可比较的 int）；
    prev 侧含 READ_FAILED / 负数 int（毒基线）→ FAIL 且报文提示人工 rebaseline，
    绝不比较放行——钉死 reviewer 的实验：`{'agents': 0}` vs prev `{'agents': -1}`
    在旧实现下 `cur >= -1` 恒真会放行整表清空。
"""
from __future__ import annotations

import shutil
import sqlite3

import pytest

import fixture_factory
from cass_backup_gate import (
    LEG3_EXEMPT_TABLE,
    LEG3_READ_FAILED,
    REQUIRED_LEG3_OBJECTS,
    Leg3Result,
    _leg3_compare_census,
    leg3,
)

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd 模板"
)


def _migrate_agents_schema_text(db_path) -> None:
    """模拟一次合法 schema 迁移（如 `ALTER TABLE agents ADD COLUMN x`）对 `agents`
    DDL 文本的影响，直接改写 `sqlite_master.sql`（手法与 `fixture_factory.attack1`
    一致：`PRAGMA writable_schema`）。

    **为什么不直接跑 `ALTER TABLE agents ADD COLUMN`**：在这份由真 cass 写出的
    synth_dd 文件上，该语句稳定触发一个与本模块代码无关的 SQLite 内部 bug——
    `error in table agents after add column: near "IN": syntax error`（多次独立
    复现，`VACUUM` 后消失，说明是该 SQLite 版本对这份文件实际页布局的重解析缺陷，
    而不是 schema 逻辑问题；`VACUUM` 本身又会顺带抹掉全部表的 `IF NOT EXISTS`
    子句，污染指纹对比的干净度）。直接改写 DDL 文本能精确达到测试目的——
    「`agents` 的 schema 变了」——且不依赖这条不稳定路径。
    """
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agents'"
        ).fetchone()
        original_sql = row[0].rstrip()
        assert original_sql.endswith(")"), f"意外的 agents DDL 形态: {original_sql!r}"
        migrated_sql = original_sql[:-1] + ", migrated_x TEXT)"

        con.execute("PRAGMA writable_schema=ON")
        con.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='agents'",
            (migrated_sql,),
        )
        con.execute("PRAGMA writable_schema=RESET")
        con.commit()
    finally:
        con.close()


def _baseline(db_path) -> tuple[dict, str]:
    """在当前状态上跑一次 leg3（无 prev）拿到 census/fingerprint，当作「上一份已发布
    备份」的基线，供后续攻击测试比对。"""
    con = sqlite3.connect(str(db_path))
    try:
        result = leg3(con, prev_census=None, prev_fingerprint=None)
    finally:
        con.close()
    assert result.ok is True, f"基线本身不应 FAIL：{result.detail}"
    return result.census, result.fingerprint


# ---------------------------------------------------------------------------
# 首晚登记模式 + 健康库自比对 PASS + EXEMPT 无条件豁免
# ---------------------------------------------------------------------------


@requires_cass
def test_leg3_first_night_registers_without_baseline(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        result = leg3(con, prev_census=None, prev_fingerprint=None)
    finally:
        con.close()

    assert isinstance(result, Leg3Result)
    assert result.ok is True
    assert result.census, "首晚也必须把 census 算出来（供 sidecar 落盘）"
    assert result.fingerprint, "首晚也必须把 fingerprint 算出来（供 sidecar 落盘）"


@requires_cass
def test_leg3_pass_when_compared_against_its_own_baseline(synth_dd):
    db = synth_dd / "agent_search.db"
    prev_census, prev_fingerprint = _baseline(db)

    con = sqlite3.connect(str(db))
    try:
        result = leg3(con, prev_census=prev_census, prev_fingerprint=prev_fingerprint)
    finally:
        con.close()

    assert result.ok is True


@requires_cass
def test_leg3_fts_messages_config_always_exempt_even_when_countable(synth_dd):
    db = synth_dd / "agent_search.db"
    # 健康合成库上 fts_messages_config 其实能 COUNT 成功（不同于生产坏 DDL，spec §2.5/§2.6）。
    con = sqlite3.connect(str(db))
    countable = con.execute(f'SELECT COUNT(*) FROM "{LEG3_EXEMPT_TABLE}"').fetchone()[0]
    con.close()
    assert countable >= 0, "前置条件不成立：健康合成库的 fts_messages_config 应可 COUNT"

    con = sqlite3.connect(str(db))
    try:
        result = leg3(con, prev_census=None, prev_fingerprint=None)
    finally:
        con.close()

    assert result.census[LEG3_EXEMPT_TABLE] == "EXEMPT"


# ---------------------------------------------------------------------------
# 攻击①：必需清单报 meta 缺失 且 普查报表消失（V4）
# ---------------------------------------------------------------------------


@requires_cass
def test_leg3_attack1_missing_meta_fails_required_and_disappears_from_census(synth_dd):
    db = synth_dd / "agent_search.db"
    prev_census, prev_fingerprint = _baseline(db)
    assert "meta" in prev_census and prev_census["meta"] > 0

    fixture_factory.attack1(db)

    con = sqlite3.connect(str(db))
    try:
        result = leg3(con, prev_census=prev_census, prev_fingerprint=prev_fingerprint)
    finally:
        con.close()

    assert result.ok is False
    assert "meta" in result.detail
    assert "meta" not in result.census, "meta 应从本次普查报表里消失"


@requires_cass
def test_leg3_attack1_still_fails_required_objects_under_rebaseline(synth_dd):
    """spec §5.7：rebaseline 只关闭「与历史基线的比对」，必需对象清单永不可关。"""
    db = synth_dd / "agent_search.db"
    prev_census, prev_fingerprint = _baseline(db)

    fixture_factory.attack1(db)

    con = sqlite3.connect(str(db))
    try:
        result = leg3(
            con, prev_census=prev_census, prev_fingerprint=prev_fingerprint, rebaseline=True
        )
    finally:
        con.close()

    assert result.ok is False
    assert "meta" in result.detail


# ---------------------------------------------------------------------------
# 攻击③：agents 清空，严格不减 FAIL（V5a）
# ---------------------------------------------------------------------------


@requires_cass
def test_leg3_attack3_agents_emptied_fails_strict_nondecrease(synth_dd):
    db = synth_dd / "agent_search.db"
    prev_census, prev_fingerprint = _baseline(db)
    assert prev_census["agents"] > 0, "前置条件不成立：基线 agents 应 > 0"

    fixture_factory.attack3(db)

    con = sqlite3.connect(str(db))
    try:
        cur_agents = con.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        result = leg3(con, prev_census=prev_census, prev_fingerprint=prev_fingerprint)
    finally:
        con.close()

    assert cur_agents == 0
    assert result.ok is False
    assert "agents" in result.detail


# ---------------------------------------------------------------------------
# sources 2→1：丢一半（V5d5 形态）
# ---------------------------------------------------------------------------


@requires_cass
def test_leg3_sources_two_to_one_fails(synth_dd):
    db = synth_dd / "agent_search.db"
    prev_census, prev_fingerprint = _baseline(db)
    assert prev_census["sources"] == 2, "前置条件不成立：基线 sources 应为 2 行"

    con = sqlite3.connect(str(db))
    con.execute("DELETE FROM sources WHERE id = (SELECT id FROM sources ORDER BY id LIMIT 1)")
    con.commit()
    con.close()

    con = sqlite3.connect(str(db))
    try:
        cur_sources = con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        result = leg3(con, prev_census=prev_census, prev_fingerprint=prev_fingerprint)
    finally:
        con.close()

    assert cur_sources == 1
    assert result.ok is False
    assert "sources" in result.detail


# ---------------------------------------------------------------------------
# 新增表 → PASS（只增不减合法）：直接测纯函数，避免与 schema 指纹比对互相干扰
# ---------------------------------------------------------------------------


def test_compare_census_new_table_is_legal():
    prev = {"agents": 3, "meta": 9}
    cur = {"agents": 3, "meta": 9, "brand_new_table": 1}
    ok, _detail = _leg3_compare_census(cur, prev)
    assert ok is True


def test_compare_census_strict_nondecrease_violation_fails():
    prev = {"agents": 10}
    cur = {"agents": 9}
    ok, detail = _leg3_compare_census(cur, prev)
    assert ok is False
    assert "agents" in detail


def test_compare_census_table_disappearing_fails():
    prev = {"agents": 3, "meta": 9}
    cur = {"agents": 3}
    ok, detail = _leg3_compare_census(cur, prev)
    assert ok is False
    assert "meta" in detail


def test_compare_census_exempt_table_skipped_even_if_decreased():
    prev = {LEG3_EXEMPT_TABLE: 5, "agents": 3}
    cur = {LEG3_EXEMPT_TABLE: 0, "agents": 3}
    ok, _detail = _leg3_compare_census(cur, prev)
    assert ok is True


# ---------------------------------------------------------------------------
# schema 指纹：ALTER TABLE → FAIL 且报文含「重设基线」；rebaseline 下改为 PASS
# ---------------------------------------------------------------------------


@requires_cass
def test_leg3_schema_migration_fails_fingerprint_with_rebaseline_hint(synth_dd):
    db = synth_dd / "agent_search.db"
    prev_census, prev_fingerprint = _baseline(db)

    _migrate_agents_schema_text(db)

    con = sqlite3.connect(str(db))
    try:
        result = leg3(con, prev_census=prev_census, prev_fingerprint=prev_fingerprint)
    finally:
        con.close()

    assert result.ok is False
    assert "重设基线" in result.detail


@requires_cass
def test_leg3_schema_migration_passes_under_rebaseline(synth_dd):
    db = synth_dd / "agent_search.db"
    prev_census, prev_fingerprint = _baseline(db)

    _migrate_agents_schema_text(db)

    con = sqlite3.connect(str(db))
    try:
        result = leg3(
            con, prev_census=prev_census, prev_fingerprint=prev_fingerprint, rebaseline=True
        )
    finally:
        con.close()

    assert result.ok is True
    assert result.fingerprint != prev_fingerprint, "指纹应确实变了（否则没测到位）"


# ---------------------------------------------------------------------------
# READ_FAILED 哨兵双向 fail-closed（review 修复：-1 int 哨兵会毒化未来基线）
# ---------------------------------------------------------------------------


class _CountRaisingConnection:
    """包一层真连接：对指定表的 `SELECT COUNT(*)` 抛 `sqlite3.DatabaseError`（真实
    损坏的异常父类），其余查询原样代理。手法同 test_leg2 的 proxy。"""

    def __init__(self, real_con, table: str):
        self._real = real_con
        self._table = table

    def execute(self, sql, *args, **kwargs):
        if f'"{self._table}"' in sql and "sqlite_master" not in sql:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return self._real.execute(sql, *args, **kwargs)


@requires_cass
def test_leg3_census_read_failure_fails_and_records_string_sentinel(synth_dd):
    """(a) 非豁免、非必需清单表（tags）COUNT raise → 当晚 FAIL，census 里该表记
    `"READ_FAILED"` 字符串哨兵——绝不是可比较的 int。"""
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    proxy = _CountRaisingConnection(con, "tags")
    try:
        result = leg3(proxy, prev_census=None, prev_fingerprint=None)
    finally:
        con.close()

    assert result.ok is False, "非豁免表读失败必须当晚 FAIL，即使是首晚登记模式"
    assert "tags" in result.detail
    assert result.census["tags"] == LEG3_READ_FAILED
    assert not isinstance(result.census["tags"], int), "哨兵绝不能是可比较的 int"


@requires_cass
def test_leg3_poisoned_prev_read_failed_fails_loud_with_rebaseline_hint(synth_dd):
    """(b) prev 侧含 READ_FAILED（上一晚哨兵 roundtrip 回来）→ FAIL 且报文提示
    人工 rebaseline——毒基线必须响亮，绝不比较放行。"""
    db = synth_dd / "agent_search.db"
    prev_census, prev_fingerprint = _baseline(db)
    prev_census["agents"] = LEG3_READ_FAILED

    con = sqlite3.connect(str(db))
    try:
        result = leg3(con, prev_census=prev_census, prev_fingerprint=prev_fingerprint)
    finally:
        con.close()

    assert result.ok is False
    assert "agents" in result.detail
    assert "rebaseline" in result.detail


def test_compare_census_prev_read_failed_sentinel_fails():
    """(b) 纯函数级：prev 含 READ_FAILED → FAIL + rebaseline 提示。"""
    ok, detail = _leg3_compare_census({"agents": 5}, {"agents": LEG3_READ_FAILED})
    assert ok is False
    assert "agents" in detail
    assert "rebaseline" in detail


def test_compare_census_prev_negative_int_fails():
    """(c) 回归钉死 reviewer 的实验：prev 含 -1 这类越界 int（旧版哨兵残留 / 手改
    sidecar），`cur >= -1` 恒真的旧实现会放行整表清空——现在必须 FAIL（合法 COUNT
    不可能为负，视同毒基线）。"""
    ok, detail = _leg3_compare_census({"agents": 0}, {"agents": -1})
    assert ok is False
    assert "agents" in detail
    assert "rebaseline" in detail


def test_compare_census_cur_read_failed_sentinel_fails():
    """cur 侧非 int（本晚 READ_FAILED）→ FAIL，无法比对（双向 fail-closed 的另一半）。"""
    ok, detail = _leg3_compare_census({"agents": LEG3_READ_FAILED}, {"agents": 5})
    assert ok is False
    assert "agents" in detail


def test_compare_census_exempt_table_skipped_both_sides_regardless_of_value():
    """EXEMPT 表两侧都按名字跳过——即使两侧的值是任意哨兵 / 非法值。"""
    ok, _detail = _leg3_compare_census(
        {LEG3_EXEMPT_TABLE: LEG3_READ_FAILED, "agents": 3},
        {LEG3_EXEMPT_TABLE: -1, "agents": 3},
    )
    assert ok is True


# ---------------------------------------------------------------------------
# 必需对象清单：健康库全部可读
# ---------------------------------------------------------------------------


@requires_cass
def test_leg3_required_objects_all_readable_on_healthy_synth_dd(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        for name in REQUIRED_LEG3_OBJECTS:
            con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
    finally:
        con.close()
