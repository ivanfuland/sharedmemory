"""CASS 备份 PR1 DB 五腿门（本文件逐 task 累加：本次新增 CLI `main()`，已含腿 0-4）。

跑在本地 staging 副本（`.backup` 产物）上，spec §5 全五腿合计 < 6 秒。任一腿失败 →
不写 `COMPLETE` → exit 非零 → TG 告警（调用方职责，不在本模块）。

`infra/backup/cass/` 不是 package——同目录模块互相 import 的约定是在模块顶部
`sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` 后直接 import。

PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 偏好 / 基建拓扑 / 真实会话内容。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import sqlite3
import struct
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cass_common  # noqa: E402 — 同目录 import 约定见模块 docstring

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
    """`SELECT COUNT(*) FROM messages` / `FROM conversations` 均须 > 0，否则 FAIL。

    缺表 / schema 损坏抛的 `sqlite3.DatabaseError`（含子类 `OperationalError`）→
    受控 FAIL 不裸 crash——与腿 2/3/4 对损坏的处理同一套哲学（review 修复：缺
    `messages` 表的库曾让整个 CLI 裸 traceback 崩溃、零产物）。"""
    try:
        messages_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conversations_count = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        return LegResult(ok=False, detail=f"防呆 COUNT 失败 — {exc}")
    detail = f"messages={messages_count} conversations={conversations_count}"
    if messages_count > 0 and conversations_count > 0:
        return LegResult(ok=True, detail=detail)
    return LegResult(ok=False, detail=f"{detail}（需均 > 0）")


# ---------------------------------------------------------------------------
# 腿 1 — integrity_check 双签名 fail-closed，不看 exit code（spec §5.2）
# ---------------------------------------------------------------------------

_SIG_A_STDOUT = "ok"
_SIG_B_STDERR = "Error: stepping, database disk image is malformed (11)"
# `.fullmatch()`（见 `classify_integrity`）：这里输入是 `stdout.splitlines()`（已剥离行
# 终止符），`.match()` + `^...$` 今天等价于 fullmatch、无尾随 \n 隐患；改 fullmatch 是
# 同族硬化——彻底关掉「`$` 匹配到 trailing newline 之前」这一类 foot-gun，行为不变。
_SIG_B_LINE_RE = re.compile(r"(\*\*\* in database main \*\*\*|Page \d+: never used)")


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
        and all(_SIG_B_LINE_RE.fullmatch(line) for line in stdout_lines)
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

# 非豁免表 COUNT 失败时 census 里记的哨兵。**必须是字符串**（与 "EXEMPT" 同类），
# 绝不用可比较的 int：census 会整份 roundtrip 成下一晚的 prev，若哨兵是 -1 这类
# int，`cur >= -1` 恒真——被毒化的基线会放行一切，包括整表清空。
LEG3_READ_FAILED = "READ_FAILED"


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
    却读不动，超出 spec 已知范围）记 `LEG3_READ_FAILED` 哨兵并计入
    `read_failures`（当晚必 FAIL），不裸 crash——与腿 2 对损坏的受控处理同一套
    哲学。哨兵是字符串不是 int，理由见 `LEG3_READ_FAILED` 处注释。"""
    census: dict[str, int | str] = {}
    read_failures: list[str] = []
    for name in _leg3_all_table_names(con):
        if name == LEG3_EXEMPT_TABLE:
            census[name] = "EXEMPT"
            continue
        try:
            census[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        except sqlite3.DatabaseError as exc:
            census[name] = LEG3_READ_FAILED
            read_failures.append(f'"{name}"（{exc}）')
    return census, read_failures


def _leg3_compare_census(
    census: dict[str, int | str], prev_census: dict[str, int | str]
) -> tuple[bool, str]:
    """part 2 的比对：一律严格不减（`cur >= prev`），上次存在本次不得消失。
    `LEG3_EXEMPT_TABLE` 两侧都按名字跳过（与存储的值无关）。新表（`prev_census`
    里没有）合法，不参与比较——只遍历 `prev_census` 的键。

    **双向 fail-closed，哨兵值绝不参与大小比较**：

    - prev 侧非 int、或负数 int（合法 COUNT 不可能为负）→ FAIL。基线已被毒化
      （如上一晚的 `READ_FAILED` 哨兵 roundtrip 回来、或旧版 -1 哨兵残留），
      必须响亮报「需人工 rebaseline」——绝不带着毒基线比较放行。
    - cur 侧非 int（本晚 `READ_FAILED`）→ FAIL，无法比对。
    """
    poisoned = []
    problems = []
    for name, prev_value in prev_census.items():
        if name == LEG3_EXEMPT_TABLE:
            continue
        if not isinstance(prev_value, int) or prev_value < 0:
            poisoned.append(f'"{name}"={prev_value!r}')
            continue
        if name not in census:
            problems.append(f'"{name}" 消失（上次存在，本次不存在）')
            continue
        cur_value = census[name]
        if not isinstance(cur_value, int):
            problems.append(f'"{name}" 本次读取失败（{cur_value!r}），无法比对')
            continue
        if cur_value < prev_value:
            problems.append(f'"{name}" 行数减少：{prev_value} → {cur_value}')
    if poisoned:
        problems.insert(
            0,
            "基线含 READ_FAILED/非法值（"
            + "; ".join(poisoned)
            + "），需人工 rebaseline",
        )
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
    prev_census: dict[str, int | str] | None,
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


# ---------------------------------------------------------------------------
# 腿 4 — append-only 前缀全列摘要 + 单调性 + meta 水位（spec §5.5）；
# rebaseline 出口见 spec §5.7。
# ---------------------------------------------------------------------------

# 覆盖对象：仅 messages 与 conversations（append-only，id 主键，spec §5.5 逐字）。
TABLES_FOR_LEG4: tuple[str, ...] = ("messages", "conversations")

# spec §5.5(a) 必需水位键硬编码清单——缺任一即 FAIL，rebaseline 也不豁免
# （与硬编码不变式的比对永不可关，spec §5.7）。
REQUIRED_LEG4_WATERMARK_KEYS: tuple[str, ...] = (
    "last_scan_ts",
    "last_scan_ts:connector:claude",
    "last_scan_ts:connector:codex",
    "last_scan_ts:connector:gemini",
    "last_scan_ts:connector:openclaw",
    "last_scan_ts:connector:pi_agent",
    "last_embedded_message_id",
    "last_indexed_at",
    "schema_version",
)

# `.fullmatch()`（不是 `.match()`）：Python 的 `$` 会匹配到 trailing newline **之前**，
# 故 `^[0-9]+$` + `.match()` 对 `"1783605600227\n"` 过匹配、`int()` 还能成功 → spec-
# invalid 的水位（含尾随 \n）被当合法整数放行（codex R7-P0 真库复现；与 Task 8 tier0
# 的 blob hex `.match()`→`.fullmatch()` 完全同源）。spec §5.5(b)/§11：先过 `^[0-9]+$`、
# 解析失败即 FAIL，且按十进制整数比较。fullmatch 锚定整串，尾随 \n / 前导 \n / 内嵌
# 空格一律拒。
_LEG4_UINT_RE = re.compile(r"[0-9]+")


def _enc(v):
    """spec §5.5 逐字：单射长度前缀编码，无分隔符。存储类以 SQLite 的 typeof() 为准
    （即本函数的 isinstance 判断作用在 sqlite3 驱动已还原出的 python 值上）。"""
    if v is None:            return b"\x00"
    if isinstance(v, int):   d = str(v).encode();   return b"i" + struct.pack(">Q", len(d)) + d
    if isinstance(v, float): return b"r" + struct.pack(">d", v)
    if isinstance(v, str):   d = v.encode("utf-8"); return b"t" + struct.pack(">Q", len(d)) + d
    d = bytes(v);            return b"b" + struct.pack(">Q", len(d)) + d


def prefix_digests(con, table, prev_max_id):
    """单遍流：返回 (digest_at_prev_max, digest_at_cur_max, cur_max, cur_count)。
    hashlib .copy() 在越过 prev_max_id 边界时留存前缀摘要 —— messages 4 s 只跑一遍。

    spec §5.5 逐字（= 附录 A 探针同构，见 `tests/backup/reference_digest_probe.py`）。
    """
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    h = hashlib.sha256(); h.update(struct.pack(">Q", len(cols)))
    for c in cols:
        d = c.encode("utf-8"); h.update(struct.pack(">Q", len(d))); h.update(d)
    h_pre, cur_max, cnt = None, 0, 0
    for row in con.execute(f'SELECT * FROM "{table}" ORDER BY id'):
        if prev_max_id is not None and h_pre is None and row[0] > prev_max_id:
            h_pre = h.copy()                      # 越过基线边界，留存前缀摘要
        h.update(struct.pack(">Q", len(row)))
        for v in row: h.update(_enc(v))
        cur_max, cnt = row[0], cnt + 1
    if prev_max_id is not None and h_pre is None:
        h_pre = h.copy()                          # cur_max == prev_max（当晚无新增）
    return ((h_pre or h).hexdigest(), h.hexdigest(), cur_max, cnt)


@dataclass
class Leg4Result:
    """腿 4 结果。`tables`/`meta_watermarks` 无论 `ok` 为何都会被算出，供 sidecar
    落盘（下一晚拿今晚的 `tables[表名]` 整份当 `prev_tables` 传入）。

    `tables`：`{表名: {"max_id": int, "count": int, "prefix_digest": str}}`——
    `prefix_digest` 是**今晚的全量摘要**（`digest_at_cur_max`），不是 `digest_at_prev_max`。
    `meta_watermarks`：`{水位键: 字符串值}`，只含 `REQUIRED_LEG4_WATERMARK_KEYS`
    里实际存在的键。
    """

    ok: bool
    detail: str
    tables: dict[str, dict[str, int | str]]
    meta_watermarks: dict[str, str]


def _leg4_parse_uint(value) -> int | None:
    """`fullmatch([0-9]+)` 校验后解析为无符号整数；不满足（含尾随/前导 `\\n`、内嵌
    空格）或非字符串一律返回 None（调用方按「解析失败即 FAIL」处理，不是跳过）。"""
    if not isinstance(value, str) or not _LEG4_UINT_RE.fullmatch(value):
        return None
    return int(value)


def _leg4_watermarks(
    con, prev_watermarks: dict[str, str] | None, rebaseline: bool
) -> tuple[list[str], dict[str, str]]:
    """meta 水位三部分——(a)/(b) 是【硬编码不变式·无条件】，(c) 是【与 baseline 的
    比较·可豁免】（codex R9-P0：格式校验此前被错误耦合进 (c) 分支，被 rebaseline/
    首晚连带豁免了）：

    - (a) 必需键存在（硬编码清单，缺任一 FAIL）——**rebaseline / 首晚 / 正常三分支
      都跑**。
    - (b) 值格式合法性：每个**存在的**必需键的值必须过 `fullmatch([0-9]+)`（合法
      十进制无符号整数，无前导/尾随空白或换行），解析失败即 FAIL——**rebaseline /
      首晚 / 正常三分支都跑**。spec §5.5(b)「每个键先过 `^[0-9]+$`，解析失败即
      FAIL」是**无条件**的，不依赖 baseline（值是不是合法整数，跟有没有上一份、
      是不是 rebaseline 无关）。
    - (c) 单调不减（`cur >= prev`，按无符号整数比较）——这是【与历史 baseline 的
      比较】，`rebaseline=True` 或 `prev_watermarks is None`（首晚）时**跳过**
      （spec §5.7/§11：rebaseline/首晚 只豁免与历史 baseline 的比较）。

    `meta` 表本身整体不可读（如攻击①用 `writable_schema` 删掉其 schema 条目）时
    `SELECT` 会抛 `sqlite3.DatabaseError`——与腿 3 对缺表的受控处理同一套哲学：
    不裸 crash，按「一个键都读不到」处理，走既有的必需键缺失 FAIL 路径（Task 7
    CLI 首次五腿串联跑通攻击①时发现：`meta` 整表消失不只是腿 3 的事）。"""
    placeholders = ",".join("?" * len(REQUIRED_LEG4_WATERMARK_KEYS))
    problems: list[str] = []
    try:
        rows = con.execute(
            f"SELECT key, value FROM meta WHERE key IN ({placeholders})",
            REQUIRED_LEG4_WATERMARK_KEYS,
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        problems.append(f'"meta" 不可读（{exc}），必需水位键视为全部缺失')
        rows = []
    current = dict(rows)

    # (a) 必需键存在——无条件。
    missing = [key for key in REQUIRED_LEG4_WATERMARK_KEYS if key not in current]
    if missing:
        problems.append("必需水位键缺失: " + ", ".join(missing))

    # (b) 值格式合法性——无条件（rebaseline/首晚也跑）。对每个存在的必需键的值过
    # `fullmatch([0-9]+)`；解析失败即 FAIL。通过的存进 current_ints 供 (c) 复用，
    # 避免重复解析。（缺失的键已在 (a) 报过，这里只校验存在键的值格式。）
    current_ints: dict[str, int] = {}
    for key in REQUIRED_LEG4_WATERMARK_KEYS:
        if key not in current:
            continue
        parsed = _leg4_parse_uint(current[key])
        if parsed is None:
            problems.append(
                f'水位键 "{key}" 解析失败（值={current[key]!r} 非合法十进制整数）'
            )
        else:
            current_ints[key] = parsed

    # (c) 单调不减——仅在有 prev 且非 rebaseline 时跑（与历史 baseline 的比较）。
    if not rebaseline and prev_watermarks is not None:
        for key in REQUIRED_LEG4_WATERMARK_KEYS:
            if key not in current_ints or key not in prev_watermarks:
                continue
            prev_int = _leg4_parse_uint(prev_watermarks[key])
            if prev_int is None:
                # 基线侧水位值非法 = 毒基线，响亮报「需人工 rebaseline」（绝不带毒
                # 基线比较放行）——与 leg3 `_leg3_compare_census` 的毒基线处置同族。
                problems.append(
                    f'基线水位键 "{key}" 解析失败（值={prev_watermarks[key]!r} 非合法'
                    "十进制整数），需人工 rebaseline"
                )
                continue
            if current_ints[key] < prev_int:
                problems.append(f'水位键 "{key}" 回退（{prev_int} → {current_ints[key]}）')

    return problems, current


def leg4(
    con,
    prev_tables: dict[str, dict] | None,
    prev_watermarks: dict[str, str] | None,
    rebaseline: bool = False,
) -> Leg4Result:
    """腿 4 — append-only 前缀全列摘要 + 单调性 + meta 水位（spec §5.5）。

    每张表（`TABLES_FOR_LEG4`）三道前置自检，任一不满足 ⇒ FAIL 不是跳过：
    1. gap=0：`COUNT(*) == MAX(id)`（空表 MAX=0/COUNT=0 视为 gap 0，见 `prefix_digests`
       的初值）。**始终执行，`rebaseline` 不豁免。**
    2. `MAX(id) >= prev.max_id`。
    3. `COUNT(*) >= prev.count`。
    外加前缀摘要比对：本晚重算 `prev.max_id` 处的前缀（`digest_at_prev_max`）必须
    与 `prev.prefix_digest` 逐字节相等。

    `rebaseline=True`：跳过 2/3 与摘要比对（与 prev 的比对整体关闭），但 1 照跑。
    `prev_tables is None`（首晚）：登记模式，2/3 与摘要比对天然跳过（无基线可比），
    `ok` 只取决于 1 + meta 必需水位键存在。

    `meta` 水位检查见 `_leg4_watermarks`：(a) 必需键存在 + (b) 值格式合法
    （`fullmatch([0-9]+)`）都是**无条件不变式**，rebaseline/首晚也照跑（codex R9-P0）；
    只有 (c) 单调不减是与历史 baseline 的比较，`rebaseline` 或首晚时跳过。
    """
    problems: list[str] = []
    tables: dict[str, dict[str, int | str]] = {}

    for table in TABLES_FOR_LEG4:
        prev_entry = prev_tables.get(table) if prev_tables is not None else None
        # rebaseline 下不读旧基线的 max_id（spec §12 B10）：rebaseline 整体丢弃旧基线、
        # 以当前 db 重建，与 prev 的单调性/前缀比对已被下方 `if not rebaseline` 跳过，
        # digest_at_prev_max 也算了不用；旧 digest 缺 `max_id` 键时无条件下标会 KeyError
        # → 经 _safe 变成假 [leg 4] FAIL，误拒合法 rebaseline。置 None 即避开且零行为变化。
        prev_max_id = (
            None if rebaseline
            else (prev_entry["max_id"] if prev_entry is not None else None)
        )

        digest_at_prev_max, digest_at_cur_max, cur_max, cnt = prefix_digests(
            con, table, prev_max_id
        )
        tables[table] = {
            "max_id": cur_max,
            "count": cnt,
            "prefix_digest": digest_at_cur_max,
        }

        if cnt != cur_max:
            problems.append(
                f'"{table}": gap 自检 FAIL（COUNT={cnt} != MAX(id)={cur_max}）'
            )

        if not rebaseline and prev_entry is not None:
            if cur_max < prev_entry["max_id"]:
                problems.append(
                    f'"{table}": max_id 回退（{prev_entry["max_id"]} → {cur_max}）'
                )
            if cnt < prev_entry["count"]:
                problems.append(
                    f'"{table}": count 回退（{prev_entry["count"]} → {cnt}）'
                )
            if digest_at_prev_max != prev_entry["prefix_digest"]:
                problems.append(f'"{table}": 前缀摘要不符（历史前缀被改写）')

    watermark_problems, meta_watermarks = _leg4_watermarks(
        con, prev_watermarks, rebaseline
    )
    problems.extend(watermark_problems)

    detail_parts: list[str] = []
    if rebaseline:
        detail_parts.append(
            "rebaseline=True：跳过与 prev 的摘要/单调性/水位单调性比对"
            "（gap 自检与必需水位键存在照跑）"
        )
    elif prev_tables is None:
        detail_parts.append("首晚登记：无历史基线，tables/meta_watermarks 已记录")

    if problems:
        detail_parts.append("; ".join(problems))
        ok = False
    else:
        ok = True
        detail_parts.append(
            "; ".join(
                f"{name}: max_id={info['max_id']} count={info['count']}"
                for name, info in tables.items()
            )
        )

    return Leg4Result(
        ok=ok, detail="; ".join(detail_parts), tables=tables, meta_watermarks=meta_watermarks
    )


# ---------------------------------------------------------------------------
# CLI —— 五腿门组装 + rebaseline 校验（Task 7，spec §5.7）
# ---------------------------------------------------------------------------


def _read_census_tsv(path) -> dict[str, int | str]:
    """读 `census.tsv`（每行 `表名\\t值`）。数值型解析为 int；`EXEMPT`/
    `LEG3_READ_FAILED` 等哨兵字符串解析失败原样保留为字符串——与 `_leg3_census`
    写出的类型形态（`int | str`）逐一对应，不需要在这里硬编码哨兵字面量。"""
    census: dict[str, int | str] = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        name, value = line.split("\t", 1)
        try:
            census[name] = int(value)
        except ValueError:
            census[name] = value
    return census


def _census_tsv_bytes(census: dict[str, int | str]) -> bytes:
    """按表名排序写 `表名\\t值` 行，确定性字节（同一份 census 任何时候序列化结果
    逐字节相等——`census_sha256` 参与链哈希，必须确定性）。"""
    lines = [f"{name}\t{census[name]}\n" for name in sorted(census)]
    return "".join(lines).encode("utf-8")


def _validate_rebaseline_target(
    dest: pathlib.Path, target_name: str, tip_name: str | None
) -> str | None:
    """spec §5.7 三项校验：① 目录存在；② 含 `COMPLETE`；③ 是链 tip（`generation`
    最大者，由调用方传入的 `latest_published` 结果判定，**不看 mtime**）。

    全部满足返回 `None`；任一不满足返回人读得懂的错误信息（调用方据此 exit 2）。
    """
    target_dir = dest / target_name
    if not target_dir.is_dir():
        return f"rebaseline 目标不存在: {target_dir}"
    if not (target_dir / "COMPLETE").exists():
        return f"rebaseline 目标缺少 COMPLETE marker（未发布完成）: {target_dir}"
    if tip_name is None or target_name != tip_name:
        return (
            f"rebaseline 目标不是链 tip（不看 mtime，只看 generation）："
            f"指名={target_name!r}，当前 tip={tip_name!r}"
        )
    return None


# codex R2-P0 修复：基线的 census.tsv/digest.json 供腿 3/4 消费的子结构必须
# 「全有或全无」——见 `_validate_baseline` docstring。
# 用 `.fullmatch()`（见下方 `_validate_baseline`）：`prefix_digest` 来自基线 digest.json
# （对抗信道），`^...$` + `.match()` 会放过「64 hex + 尾随 \n」这类 spec-invalid 值
# 进结构门（codex R7-P0 同族），须整串锚定。
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


def _validate_baseline(
    dest: pathlib.Path, prev_name: str, prev_digest: dict
) -> str | None:
    """非 rebaseline 模式下、基线存在时的「全有或全无」校验（codex R2-P0：单点
    删除基线的一个字段/一行会让腿 3/4「只比对 prev 里存在的键」的逻辑悄悄跳过
    对应检查，制造静默降级成「无基线」的假绿——三种复现：删 census.tsv 里一行
    再清空当前对应表、删 digest.json 的 tables.messages 再改写内容、删
    meta_watermarks 的一个必需键再回退水位，三者都曾 rc=0）。

    校验内容（任一不满足即失败，返回人读得懂的错误信息；全部满足返回
    `None`）：

    1. `<dest>/<prev_name>/census.tsv` 的原始字节 sha256 必须与
       `prev_digest["census_sha256"]` 一致（文件缺失同样判失败）——防行级篡改。
    2. `prev_digest` 供腿 3/4 消费的子结构完整性：
       - `schema_fingerprint`：非空字符串。
       - `tables[table]`（`table` ∈ `TABLES_FOR_LEG4`）：`max_id`/`count` 均为
         非负 int，`prefix_digest` 为 64 位十六进制字符串。
       - `meta_watermarks`：含 `REQUIRED_LEG4_WATERMARK_KEYS` 全部键（与腿 4
         必需键清单同源 import，不在这里抄第二份）。

    调用方（`main()`）据此判定：校验失败 ⇒ 不把 prev_census/prev_tables/
    prev_watermarks 喂给腿 3/4（视同无基线，避免用可能同样残缺的结构继续算），
    但仍跑完全部五腿并写产物（SUSPECT 取证需要完整画像），只是整体 exit code
    额外受这次校验结果约束——不能悄悄退化成「首晚登记」式的 ok=True。
    """
    census_path = dest / prev_name / "census.tsv"
    if not census_path.is_file():
        return (
            f"基线 census.tsv 缺失: {census_path}（基线损坏/被改，需人工 rebaseline）"
        )
    actual_sha256 = hashlib.sha256(census_path.read_bytes()).hexdigest()
    expected_sha256 = prev_digest.get("census_sha256")
    if actual_sha256 != expected_sha256:
        return (
            f"基线 census.tsv 与 digest.json 记录的 census_sha256 不符"
            f"（实际={actual_sha256}，记录={expected_sha256!r}）"
            "——基线损坏/被改，需人工 rebaseline"
        )

    fingerprint = prev_digest.get("schema_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return (
            "基线 digest.json 缺 schema_fingerprint（应为非空字符串）"
            "——基线损坏/被改，需人工 rebaseline"
        )

    tables = prev_digest.get("tables")
    if not isinstance(tables, dict):
        return "基线 digest.json 缺 tables（应为 dict）——基线损坏/被改，需人工 rebaseline"
    for table_name in TABLES_FOR_LEG4:
        entry = tables.get(table_name)
        if not isinstance(entry, dict):
            return (
                f'基线 digest.json 的 tables["{table_name}"] 缺失或不是 dict'
                "——基线损坏/被改，需人工 rebaseline"
            )
        max_id = entry.get("max_id")
        count = entry.get("count")
        prefix_digest = entry.get("prefix_digest")
        if type(max_id) is not int or max_id < 0:
            return (
                f'基线 digest.json 的 tables["{table_name}"].max_id 非法'
                f"（{max_id!r}，应为非负 int）——基线损坏/被改，需人工 rebaseline"
            )
        if type(count) is not int or count < 0:
            return (
                f'基线 digest.json 的 tables["{table_name}"].count 非法'
                f"（{count!r}，应为非负 int）——基线损坏/被改，需人工 rebaseline"
            )
        if not isinstance(prefix_digest, str) or not _HEX64_RE.fullmatch(prefix_digest):
            return (
                f'基线 digest.json 的 tables["{table_name}"].prefix_digest 非法'
                f"（{prefix_digest!r}，应为 64 位十六进制字符串）"
                "——基线损坏/被改，需人工 rebaseline"
            )

    watermarks = prev_digest.get("meta_watermarks")
    if not isinstance(watermarks, dict):
        return (
            "基线 digest.json 缺 meta_watermarks（应为 dict）"
            "——基线损坏/被改，需人工 rebaseline"
        )
    missing_keys = [key for key in REQUIRED_LEG4_WATERMARK_KEYS if key not in watermarks]
    if missing_keys:
        return (
            "基线 digest.json 的 meta_watermarks 缺必需键: "
            + ", ".join(missing_keys)
            + "——基线损坏/被改，需人工 rebaseline"
        )

    return None


def main(argv: list[str] | None = None) -> int:
    """五腿门 CLI：跑腿 0→1→2→3→4（顺序固定，不短路），产出 `census.tsv` +
    `gate.json`（无论 PASS/FAIL 都写——SUSPECT 取证需要完整画像），返回
    0=PASS / 1=FAIL（任一腿，或非 rebaseline 模式下基线本身校验不通过，见
    `_validate_baseline`） / 2=用法或环境错误（含 rebaseline 目标校验失败）。
    """
    parser = argparse.ArgumentParser(prog="cass_backup_gate.py")
    parser.add_argument("--db", required=True, help="staging 副本 db 路径")
    parser.add_argument("--dest", required=True, help="已发布备份的 DEST 根目录")
    parser.add_argument("--out-census", required=True, dest="out_census")
    parser.add_argument("--out-gate-json", required=True, dest="out_gate_json")
    parser.add_argument("--rebaseline", default=None)
    parser.add_argument("--rebaseline-reason", default=None, dest="rebaseline_reason")
    args = parser.parse_args(argv)

    # 人工通道成对性校验（CLI 双保险；bash 层的成对校验见 Task 9）：缺一即拒绝运行。
    # 注意 `bool()` 让空字符串（`--rebaseline ""`）与「未提供」同义：混搭（一空
    # 一非空）会在这里被拒绝（fail-closed），两个都空则整体当「无 rebaseline」
    # 处理——这不是巧合而是有意选择：spec §5.7 要求 reason 非空，空串没有资格
    # 当作「已提供」。
    if bool(args.rebaseline) != bool(args.rebaseline_reason):
        print(
            "错误: --rebaseline 与 --rebaseline-reason 必须成对提供，缺一即拒绝运行",
            file=sys.stderr,
        )
        return 2

    rebaseline = bool(args.rebaseline)
    dest = pathlib.Path(args.dest)

    # codex R4-P0：基线选择必须 strict——若基线集里有「含 COMPLETE 但 generation
    # 不可读/非法」的成员，真实链 tip 不可信，绝不像轮转那样宽容 skip 掉它、静默
    # 退回更老的一份比对（那会把相对真实上一份缩水的坏备份当好备份放行）。视同
    # 「基线不可信」：不喂 prev 给腿 3/4（无比对）、五腿照跑、产物照写（SUSPECT
    # 取证）、强制 exit 1。连 rebaseline 也不放行——tip 都算不出，rebaseline 目标
    # 的 tip 校验没有意义，须人工先修/清理该成员再来。
    baseline_error: str | None = None
    try:
        baseline = cass_common.latest_published(dest, strict=True)
    except cass_common.PublishedScanError as exc:
        baseline = None
        baseline_error = str(exc)

    if baseline is None:
        prev_name, prev_digest = None, None
    else:
        prev_name, prev_digest = baseline

    if rebaseline and baseline_error is None:
        error = _validate_rebaseline_target(dest, args.rebaseline, prev_name)
        if error is not None:
            print(f"错误: {error}", file=sys.stderr)
            return 2

    # codex R2-P0：非 rebaseline 模式下、基线存在时先做「全有或全无」校验——校验
    # 失败就不能把（可能同样残缺的）prev_census/prev_tables/prev_watermarks 喂给
    # 腿 3/4，那样只会把基线损坏悄悄传播成「比对不到对应键就跳过」的假绿（见
    # `_validate_baseline` docstring 的三种复现）。rebaseline 模式跳过本校验——
    # 毒基线的合法逃生门，rebaseline 本来就只与硬编码不变式比（spec §5.7）。
    if baseline_error is None and prev_name is not None and not rebaseline:
        baseline_error = _validate_baseline(dest, prev_name, prev_digest)

    # rebaseline 下**根本不物化旧基线**（spec §12 B10，完整修）：rebaseline 的语义就是
    # 丢弃旧基线、以当前 db 重建，腿 3/4 的 rebaseline 分支本就跳过与 prev 的一切比对。
    # `_validate_rebaseline_target` 只查目录/COMPLETE/tip，不查 digest 内容良构——半成品
    # 旧基线（缺 census.tsv / `tables` 非 dict / 缺 max_id 键）若被物化，`_read_census_tsv`
    # 会在下方 `_safe` 之外直接抛（CLI 裸崩），或喂进腿 3/4 触发假 FAIL，误拒合法 rebaseline。
    # 全置 None 一次根治整类（prev_name 仍保留用于 chain/审计留痕，在别处消费）。
    if prev_name is None or baseline_error is not None or rebaseline:
        prev_census: dict[str, int | str] | None = None
        prev_fingerprint: str | None = None
        prev_tables: dict[str, dict] | None = None
        prev_watermarks: dict[str, str] | None = None
    else:
        prev_census = _read_census_tsv(dest / prev_name / "census.tsv")
        prev_fingerprint = prev_digest.get("schema_fingerprint")
        prev_tables = prev_digest.get("tables")
        prev_watermarks = prev_digest.get("meta_watermarks")

    db_path = pathlib.Path(args.db)
    if not db_path.is_file():
        # 显式存在性检查——`immutable=1` URI 对不存在的文件不会在 connect() 时
        # 报错，而是静默打开一个空 schema（首条 `SELECT` 才会报 "no such table"，
        # 且不受下面 try/except 保护），会让 CLI 以裸 traceback 崩溃而不是走干净
        # 的 exit 2 环境错误路径。
        print(f"错误: --db 路径不存在或不是文件: {db_path}", file=sys.stderr)
        return 2

    try:
        con = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
    except sqlite3.Error as exc:
        print(f"错误: 无法打开 db（{db_path}）: {exc}", file=sys.stderr)
        return 2

    def _safe(leg_fn, fallback_factory):
        """顶层安全网（review 修复）：任何单腿抛出**任何异常**（含 leg1 的
        subprocess 环境类失败）→ 该腿降级为 FAIL（detail 含异常类型+文本），
        其余腿照跑、产物照写、exit 1。这是「SUSPECT 取证需完整画像」契约的
        最后防线——单腿崩溃绝不允许击穿「不短路 + 产物无论 PASS/FAIL 都写」
        的落盘承诺。各腿内部的 `sqlite3.DatabaseError` 受控处理仍是第一道防线
        （detail 更精准）；这里兜的是它们没料到的一切。"""
        try:
            return leg_fn()
        except Exception as exc:  # noqa: BLE001 — 最后防线，故意宽
            return fallback_factory(f"腿内部异常（{type(exc).__name__}: {exc}）")

    def _leg1():
        stdout, stderr, exit_code = run_integrity_check(args.db)
        sig = classify_integrity(stdout, stderr, exit_code)
        return LegResult(ok=sig in ("A", "B"), detail=f"integrity_check signature={sig}")

    def _leg_fail(detail: str) -> LegResult:
        return LegResult(ok=False, detail=detail)

    try:
        r0 = _safe(lambda: leg0(con), _leg_fail)
        r1 = _safe(_leg1, _leg_fail)
        r2 = _safe(lambda: leg2(con), _leg_fail)
        r3 = _safe(
            lambda: leg3(con, prev_census, prev_fingerprint, rebaseline=rebaseline),
            lambda detail: Leg3Result(ok=False, detail=detail, census={}, fingerprint=""),
        )
        r4 = _safe(
            lambda: leg4(con, prev_tables, prev_watermarks, rebaseline=rebaseline),
            lambda detail: Leg4Result(ok=False, detail=detail, tables={}, meta_watermarks={}),
        )
    finally:
        con.close()

    leg_results = [r0, r1, r2, r3, r4]
    for i, r in enumerate(leg_results):
        print(f"[leg {i}] {'PASS' if r.ok else 'FAIL'}: {r.detail}")
    if baseline_error is not None:
        # 独立于五腿之外打印（不是某条腿的 detail）：这是「基线本身不可信」，
        # 不是「当前 db 的数据不达标」——两件事分开报，但都必须让整体 exit
        # 非零（见下方 return）。census.tsv/gate.json 仍照常写（用本次 db 现算
        # 的 census/fingerprint/tables/watermarks，SUSPECT 取证需要完整画像，
        # 也可能是人工核实后拿来做下一次 --rebaseline 目标的候选）。
        print(f"[baseline] FAIL: {baseline_error}")

    census_bytes = _census_tsv_bytes(r3.census)
    pathlib.Path(args.out_census).write_bytes(census_bytes)
    census_sha256 = hashlib.sha256(census_bytes).hexdigest()

    gate: dict = {
        "schema_fingerprint": r3.fingerprint,
        "tables": r4.tables,
        "meta_watermarks": r4.meta_watermarks,
        "census_sha256": census_sha256,
    }
    if rebaseline:
        gate["rebaselined_from"] = args.rebaseline
        gate["reason"] = args.rebaseline_reason

    pathlib.Path(args.out_gate_json).write_bytes(cass_common.dumps_canonical(gate))

    return 0 if all(r.ok for r in leg_results) and baseline_error is None else 1


if __name__ == "__main__":
    sys.exit(main())
