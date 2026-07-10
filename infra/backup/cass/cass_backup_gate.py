"""CASS 备份 PR1 DB 五腿门（本文件逐 task 累加：本次只落腿 0 + 腿 1）。

跑在本地 staging 副本（`.backup` 产物）上，spec §5 全五腿合计 < 6 秒。任一腿失败 →
不写 `COMPLETE` → exit 非零 → TG 告警（调用方职责，不在本模块）。

`infra/backup/cass/` 不是 package——同目录模块互相 import 的约定是在模块顶部
`sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` 后直接 import。

PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 偏好 / 基建拓扑 / 真实会话内容。
"""
from __future__ import annotations

import re
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
