"""CASS 备份 PR1 DB 五腿门（本文件逐 task 累加：本次新增腿 2，已含腿 0 + 腿 1 + 腿 2）。

跑在本地 staging 副本（`.backup` 产物）上，spec §5 全五腿合计 < 6 秒。任一腿失败 →
不写 `COMPLETE` → exit 非零 → TG 告警（调用方职责，不在本模块）。

`infra/backup/cass/` 不是 package——同目录模块互相 import 的约定是在模块顶部
`sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` 后直接 import。

PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 偏好 / 基建拓扑 / 真实会话内容。
"""
from __future__ import annotations

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

# 生产库唯一已知豁免（与腿 3 的豁免一致，§2.6）：`fts_messages_config` 因一次
# `.recover` 式重建丢了本该有的 `WITHOUT ROWID`，被下面的 classify 误判成普通
# rowid 表，实测其查询必报 `database disk image is malformed`。健康库里这张表
# 本身带 `WITHOUT ROWID`，会在 `_leg2_rowid_tables` 就被跳过、根本走不到这条
# 豁免路径——该豁免只在「被误判为 rowid 表 + 确实 malformed」时生效，换成任何
# 别的表名报同样的错都必须 FAIL，不得静默放行。
_LEG2_MALFORMED_EXEMPT_TABLE = "fts_messages_config"
_LEG2_MALFORMED_MARKER = "malformed"


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
    `fts_messages_config` 若被误判为 rowid 表且查询报 malformed，按已知生产库
    缺陷豁免（且仅它，§2.6）；其余任何表查询失败一律 FAIL。
    """
    checked: list[str] = []
    for table in _leg2_rowid_tables(con):
        query = LEG2_SQL.format(table=table)

        try:
            eqp_text = _leg2_eqp_text(con, query)
        except sqlite3.OperationalError as exc:
            if (
                table == _LEG2_MALFORMED_EXEMPT_TABLE
                and _LEG2_MALFORMED_MARKER in str(exc)
            ):
                checked.append(f"{table}=豁免（EQP {exc}）")
                continue
            return LegResult(ok=False, detail=f'"{table}": EXPLAIN QUERY PLAN 失败 — {exc}')

        if not _leg2_eqp_self_certifies(eqp_text):
            return LegResult(
                ok=False,
                detail=f'"{table}": EQP 自证失败（{eqp_text}）',
            )

        try:
            diff_count = con.execute(query).fetchone()[0]
        except sqlite3.OperationalError as exc:
            if (
                table == _LEG2_MALFORMED_EXEMPT_TABLE
                and _LEG2_MALFORMED_MARKER in str(exc)
            ):
                checked.append(f"{table}=豁免（主查询 {exc}）")
                continue
            return LegResult(ok=False, detail=f'"{table}": 主查询失败 — {exc}')

        if diff_count != 0:
            return LegResult(ok=False, detail=f'"{table}": scan-vs-seek 分歧={diff_count}')

        checked.append(f"{table}=0")

    return LegResult(ok=True, detail="; ".join(checked))
