"""CASS 备份 PR1 DB 五腿门（本文件逐 task 累加：本次新增腿 2，已含腿 0 + 腿 1 + 腿 2）。

跑在本地 staging 副本（`.backup` 产物）上，spec §5 全五腿合计 < 6 秒。任一腿失败 →
不写 `COMPLETE` → exit 非零 → TG 告警（调用方职责，不在本模块）。

`infra/backup/cass/` 不是 package——同目录模块互相 import 的约定是在模块顶部
`sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` 后直接 import。

PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 偏好 / 基建拓扑 / 真实会话内容。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# 共享形态：五腿门每一腿的判定结果，后续腿（2/3/4）复用同一形态。
# ---------------------------------------------------------------------------


@dataclass
class LegResult:
    """`ok`：该腿是否 PASS。`detail`：人读得懂的判定依据（日志 / 告警文案用）。"""

    ok: bool
    detail: str


# ---------------------------------------------------------------------------
# 腿 0 — 防呆：拦「count == 0 即通过」这一整类假绿（spec §5.1）
# ---------------------------------------------------------------------------


def leg0(con) -> LegResult:
    """`SELECT COUNT(*) FROM messages` / `FROM conversations` 均须 > 0，否则 FAIL。"""
    messages_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conversations_count = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    detail = f"messages={messages_count} conversations={conversations_count}"
    if messages_count > 0 and conversations_count > 0:
        return LegResult(ok=True, detail=detail)
    return LegResult(ok=False, detail=f"{detail}（需均 > 0）")


# ---------------------------------------------------------------------------
# 腿 1 — integrity_check 双签名 fail-closed，不看 exit code（spec §5.2）
# ---------------------------------------------------------------------------

_SIG_A_STDOUT = "ok"
_SIG_B_STDERR = "Error: stepping, database disk image is malformed (11)"
_SIG_B_LINE_RE = re.compile(r"^(\*\*\* in database main \*\*\*|Page \d+: never used)$")


def classify_integrity(stdout: str, stderr: str, exit_code: int) -> Literal["A", "B", "FAIL"]:
    """PASS 当且仅当精确匹配签名 A 或签名 B；其余一切 FAIL（未知输出形态 = FAIL，不是 warn）。

    - 签名 A：stdout 恰好一行 `ok`、stderr 空、exit 0。
    - 签名 B：stdout 每一行都匹配 `*** in database main ***` 或 `Page N: never used`
      （至少一行），且 stderr 恰好是「database disk image is malformed」那一行。
      **签名 B 不看 exit code。**
    """
    stdout_lines = stdout.splitlines()

    if stdout_lines == [_SIG_A_STDOUT] and stderr == "" and exit_code == 0:
        return "A"

    if (
        stdout_lines
        and all(_SIG_B_LINE_RE.match(line) for line in stdout_lines)
        and stderr.rstrip("\n") == _SIG_B_STDERR
    ):
        return "B"

    return "FAIL"


def run_integrity_check(db_path) -> tuple[str, str, int]:
    """以 `immutable=1` URI 打开快照，经 sqlite3 CLI 子进程跑 `PRAGMA integrity_check`，
    返回 `(stdout, stderr, exit_code)` 三元组喂 `classify_integrity`。"""
    uri = f"file:{db_path}?immutable=1"
    result = subprocess.run(
        ["sqlite3", uri, "PRAGMA integrity_check;"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout, result.stderr, result.returncode


# ---------------------------------------------------------------------------
# 腿 2 — scan-vs-seek 分歧，外层强制 `NOT INDEXED`（spec §5.3）
# ---------------------------------------------------------------------------

# 供测试注入劣化版（如去掉 `NOT INDEXED`，验证 V5e 的 EQP 自证真的会 FAIL）。
# `leg2` 在调用时按名字查模块全局，故 `monkeypatch.setattr(cass_backup_gate, "LEG2_SQL", ...)`
# 对已定义的 `leg2` 同样生效。
LEG2_SQL = (
    'SELECT COUNT(*) FROM "{table}" AS a NOT INDEXED '
    'WHERE NOT EXISTS (SELECT 1 FROM "{table}" AS b WHERE b.rowid = a.rowid)'
)

# 无豁免：NOT INDEXED + rowid seek 不经过任何二级索引，实测在生产坏 DDL 的
# fts_messages_config 上正常执行（spec §5.3 无豁免条款是深思熟虑的）。


def _leg2_rowid_tables(con) -> list[str]:
    """列出待查的 rowid 表名：跳过虚表（`sql` 含 `CREATE VIRTUAL TABLE`）与
    `WITHOUT ROWID` 表（`sql` 尾部含该关键字）。FTS5 影子表里的普通 rowid 表
    （如 `fts_messages_content`/`fts_messages_data`/`fts_messages_docsize`）
    不受虚表连坐，照常纳入。"""
    rows = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    tables = []
    for name, sql in rows:
        sql_text = (sql or "").upper()
        if "CREATE VIRTUAL TABLE" in sql_text:
            continue
        if sql_text.rstrip().endswith("WITHOUT ROWID"):
            continue
        tables.append(name)
    return tables


def _leg2_eqp_text(con, query: str) -> str:
    rows = con.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    return " | ".join(row[3] for row in rows)


def _leg2_eqp_self_certifies(eqp_text: str) -> bool:
    """spec §5.3 的查询计划自证：必须含 `SCAN a` 与
    `SEARCH b USING INTEGER PRIMARY KEY`，且不含 `COVERING INDEX`。三个子条件
    各自独立判断——任一不满足就必须 False（防未来 SQLite 把恒真的相关子查询
    优化掉，或用覆盖索引悄悄绕过 b-tree 扫描，造成永远返回 0 的静默假绿）。"""
    return (
        "SCAN a" in eqp_text
        and "SEARCH b USING INTEGER PRIMARY KEY" in eqp_text
        and "COVERING INDEX" not in eqp_text
    )


def leg2(con) -> LegResult:
    """对每张 rowid 表跑 scan-vs-seek 分歧查询，且先自证查询计划再信任其数字。

    任一表的 EQP 不满足自证条件 ⇒ 该腿 FAIL（不是跳过，spec §5.3 逐字要求）。
    任一表的 EQP 或主查询抛 `sqlite3.DatabaseError`（真实损坏抛的是这个父类，
    不只是 `OperationalError`）⇒ 受控 FAIL（detail 带表名 + 异常文本），不裸 crash。
    无表级豁免（见上方 LEG2_SQL 处注释）。
    """
    checked: list[str] = []
    for table in _leg2_rowid_tables(con):
        query = LEG2_SQL.format(table=table)

        try:
            eqp_text = _leg2_eqp_text(con, query)
        except sqlite3.DatabaseError as exc:
            return LegResult(ok=False, detail=f'"{table}": EXPLAIN QUERY PLAN 失败 — {exc}')

        if not _leg2_eqp_self_certifies(eqp_text):
            return LegResult(
                ok=False,
                detail=f'"{table}": EQP 自证失败（{eqp_text}）',
            )

        try:
            diff_count = con.execute(query).fetchone()[0]
        except sqlite3.DatabaseError as exc:
            return LegResult(ok=False, detail=f'"{table}": 主查询失败 — {exc}')

        if diff_count != 0:
            return LegResult(ok=False, detail=f'"{table}": scan-vs-seek 分歧={diff_count}')

        checked.append(f"{table}=0")

    return LegResult(ok=True, detail="; ".join(checked))


# ---------------------------------------------------------------------------
# 腿 3 — schema 与行数普查（spec §5.4）；rebaseline 出口见 spec §5.7
# ---------------------------------------------------------------------------

# 硬编码必需清单，逐字照抄 spec §5.4 part 1——不依赖 sqlite_master 的自述。
# `fts_messages` 是虚表，COUNT 走 FTS5 正常。
REQUIRED_LEG3_OBJECTS: tuple[str, ...] = (
    "agents",
    "conversations",
    "messages",
    "meta",
    "sources",
    "workspaces",
    "conversation_tail_state",
    "fts_messages",
)

# 无条件豁免且只豁免这一张：生产上它的 COUNT(*) 必报 malformed（§2.6，缺
# autoindex 的既有缺陷）。健康合成库上它其实能 COUNT 成功，但仍无条件记
# "EXEMPT"——行为确定性优先于「顺便多验一次」。
LEG3_EXEMPT_TABLE = "fts_messages_config"


@dataclass
class Leg3Result:
    """腿 3 结果。`census`/`fingerprint` 无论 `ok` 为何都会被算出，供 sidecar 落盘
    （含失败当晚的 SUSPECT 快照，人工 diff 新旧 sidecar 判断是迁移还是事故）。"""

    ok: bool
    detail: str
    census: dict[str, int | str]
    fingerprint: str


def _leg3_required_objects_check(con) -> LegResult:
    """part 1：硬编码必需清单，每张都要 `SELECT COUNT(*)` 成功。缺表 / schema 损坏
    抛的 `sqlite3.DatabaseError`（含其子类 `OperationalError`）都算缺失，不裸 crash。"""
    missing = []
    for name in REQUIRED_LEG3_OBJECTS:
        try:
            con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
        except sqlite3.DatabaseError as exc:
            missing.append(f'"{name}"（{exc}）')
    if missing:
        return LegResult(ok=False, detail="必需对象缺失或不可读: " + "; ".join(missing))
    return LegResult(
        ok=True, detail="必需对象清单全部可读: " + ", ".join(REQUIRED_LEG3_OBJECTS)
    )


def _leg3_all_table_names(con) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def _leg3_census(con) -> tuple[dict[str, int | str], list[str]]:
    """part 2 的普查本体：对每张表跑 `COUNT(*)`。`LEG3_EXEMPT_TABLE` 无条件记
    `"EXEMPT"`，不尝试 COUNT。其余表若 COUNT 抛 `sqlite3.DatabaseError`（非豁免表
    却读不动，超出 spec 已知范围）记 `-1` 并计入 `read_failures`，不裸 crash——与
    腿 2 对损坏的受控处理同一套哲学。"""
    census: dict[str, int | str] = {}
    read_failures: list[str] = []
    for name in _leg3_all_table_names(con):
        if name == LEG3_EXEMPT_TABLE:
            census[name] = "EXEMPT"
            continue
        try:
            census[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        except sqlite3.DatabaseError as exc:
            census[name] = -1
            read_failures.append(f'"{name}"（{exc}）')
    return census, read_failures


def _leg3_compare_census(
    census: dict[str, int | str], prev_census: dict[str, int | str]
) -> tuple[bool, str]:
    """part 2 的比对：一律严格不减（`cur >= prev`），上次存在本次不得消失。
    `LEG3_EXEMPT_TABLE` 按名字跳过（与存储的值无关）。新表（`prev_census` 里没有）
    合法，不参与比较——只遍历 `prev_census` 的键。"""
    problems = []
    for name, prev_value in prev_census.items():
        if name == LEG3_EXEMPT_TABLE:
            continue
        if name not in census:
            problems.append(f'"{name}" 消失（上次存在，本次不存在）')
            continue
        cur_value = census[name]
        if cur_value < prev_value:
            problems.append(f'"{name}" 行数减少：{prev_value} → {cur_value}')
    if problems:
        return False, "全表普查 FAIL: " + "; ".join(problems)
    return True, "全表普查 PASS（严格不减，无表消失）"


def _leg3_fingerprint(con) -> str:
    """part 3：`sha256(type|name|COALESCE(sql,'') ORDER BY type,name)`，行间用
    `\\n` 分隔（spec §5.4 逐字构造）。"""
    rows = con.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    lines = [f"{type_}|{name}|{sql}" for type_, name, sql in rows]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def leg3(
    con,
    prev_census: dict[str, int] | None,
    prev_fingerprint: str | None,
    rebaseline: bool = False,
) -> Leg3Result:
    """腿 3 — schema 与行数普查（spec §5.4），三部分全过才 PASS：

    1. 必需对象清单（硬编码，rebaseline 也不豁免——spec §5.7「与硬编码不变式的比对
       永不可关」）。
    2. 全表行数普查，与 `prev_census` 比对：严格不减 + 不得消失，豁免
       `LEG3_EXEMPT_TABLE`。
    3. schema 指纹与 `prev_fingerprint` 比对。

    `prev_census is None`（首晚）：登记模式，不做 2/3 比对，`ok` 只取决于第 1 部分。
    `rebaseline=True`：跳过 2/3 与 prev 的比对（但第 1 部分照跑）。
    `census`/`fingerprint` 无论结果如何都会被算出，供 sidecar 落盘。
    """
    required_result = _leg3_required_objects_check(con)
    census, census_read_failures = _leg3_census(con)
    fingerprint = _leg3_fingerprint(con)

    detail_parts = [required_result.detail]
    ok = required_result.ok

    if census_read_failures:
        detail_parts.append("普查读取失败: " + "; ".join(census_read_failures))
        ok = False

    if rebaseline:
        detail_parts.append(
            "rebaseline=True：跳过与 prev 基线的普查/指纹比对（必需对象清单不受影响）"
        )
    elif prev_census is None:
        detail_parts.append("首晚登记：无历史基线，census/fingerprint 已记录")
    else:
        census_ok, census_detail = _leg3_compare_census(census, prev_census)
        detail_parts.append(census_detail)
        ok = ok and census_ok

        fingerprint_ok = fingerprint == prev_fingerprint
        if fingerprint_ok:
            detail_parts.append("schema 指纹一致")
        else:
            detail_parts.append(
                "schema 指纹不一致（若为 CASS 的合法迁移，人工确认后重设基线）"
            )
        ok = ok and fingerprint_ok

    return Leg3Result(
        ok=ok, detail="; ".join(detail_parts), census=census, fingerprint=fingerprint
    )
