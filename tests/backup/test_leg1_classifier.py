"""infra/backup/cass/cass_backup_gate.py 的单元测试（腿 0 + 腿 1）。

覆盖 Task 3 brief 的全部测试要点：
  - classify_integrity 参数化覆盖：签名 A（干净输出）/ 签名 B（录制文本拆分）/
    若干 FAIL 分支（混入陌生行、`ok` 附带多余行、空 stdout、B 形状但 stderr 不符）
  - leg0：synth_dd 上 PASS；`messages`/`conversations` 任一清空后 FAIL
  - run_integrity_check：对 synth_dd 的 db 真跑一次 sqlite3 CLI，期望签名 A
    （合成库是干净 FTS5，marker 页不需要）

签名 B 无法用合成库复现（探针已实证，见 spec §2.4）——判定器与执行分离：
classify_integrity 用录制文本 hermetic 测判定，run_integrity_check 用真库测执行。
"""
from __future__ import annotations

import pathlib
import shutil
import sqlite3

import pytest

from cass_backup_gate import LegResult, classify_integrity, leg0, run_integrity_check

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
RECORDED_SIG_B = REPO / "tests" / "backup" / "recorded" / "integrity_sig_b.txt"

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd 模板"
)


def _split_recorded_sig_b(path: pathlib.Path) -> tuple[str, str]:
    """录制文件格式：第 1 行是 sqlite3 CLI 的 stderr；随后是它的 stdout（`*** in
    database main ***` + `Page N: never used` 行），直到 recording harness 自己
    追加的尾巴（`Command exited with non-zero status N` 起——那是录制工具的收尾
    信息，不是 sqlite3 的输出，必须剥离）。
    """
    lines = path.read_text().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    stderr = lines[0] + "\n"
    stdout_lines: list[str] = []
    for line in lines[1:]:
        if line.startswith("Command exited with non-zero status"):
            break
        stdout_lines.append(line)
    stdout = "\n".join(stdout_lines) + "\n"
    return stdout, stderr


_SIG_B_STDOUT, _SIG_B_STDERR = _split_recorded_sig_b(RECORDED_SIG_B)


# ---------------------------------------------------------------------------
# 录制文件解析自检：防止上面那段解析逻辑本身悄悄配错，掩盖掉真实的判定器 bug。
# ---------------------------------------------------------------------------


def test_recorded_sig_b_parses_to_known_shape():
    lines = _SIG_B_STDOUT.splitlines()
    assert len(lines) == 13
    assert lines[0] == "*** in database main ***"
    assert all(line.startswith("Page ") for line in lines[1:])
    assert _SIG_B_STDERR == "Error: stepping, database disk image is malformed (11)\n"


# ---------------------------------------------------------------------------
# classify_integrity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stdout, stderr, exit_code, expected",
    [
        pytest.param("ok", "", 0, "A", id="sig-a-clean"),
        pytest.param(_SIG_B_STDOUT, _SIG_B_STDERR, 11, "B", id="sig-b-recorded"),
        pytest.param(
            _SIG_B_STDOUT.rstrip("\n") + "\nRowid 905 out of order\n",
            _SIG_B_STDERR,
            11,
            "FAIL",
            id="sig-b-foreign-line-fails",
        ),
        pytest.param("ok\nok\n", "", 0, "FAIL", id="sig-a-extra-lines-fails"),
        pytest.param("", "", 0, "FAIL", id="empty-stdout-fails"),
        pytest.param(
            _SIG_B_STDOUT, "", 0, "FAIL", id="sig-b-shape-clean-stderr-fails"
        ),
    ],
)
def test_classify_integrity(stdout, stderr, exit_code, expected):
    assert classify_integrity(stdout, stderr, exit_code) == expected


# ---------------------------------------------------------------------------
# leg0
# ---------------------------------------------------------------------------


@requires_cass
def test_leg0_pass_on_synth_dd(synth_dd):
    con = sqlite3.connect(str(synth_dd / "agent_search.db"))
    try:
        result = leg0(con)
    finally:
        con.close()
    assert isinstance(result, LegResult)
    assert result.ok is True


@requires_cass
def test_leg0_fails_when_messages_emptied(synth_dd):
    con = sqlite3.connect(str(synth_dd / "agent_search.db"))
    try:
        con.execute("DELETE FROM messages")
        con.commit()
        result = leg0(con)
    finally:
        con.close()
    assert result.ok is False


@requires_cass
def test_leg0_fails_when_conversations_emptied(synth_dd):
    con = sqlite3.connect(str(synth_dd / "agent_search.db"))
    try:
        con.execute("DELETE FROM conversations")
        con.commit()
        result = leg0(con)
    finally:
        con.close()
    assert result.ok is False


# ---------------------------------------------------------------------------
# run_integrity_check（真跑 sqlite3 CLI；合成库是干净 FTS5，期望签名 A）
# ---------------------------------------------------------------------------


@requires_cass
def test_run_integrity_check_on_synth_dd_is_signature_a(synth_dd):
    stdout, stderr, exit_code = run_integrity_check(synth_dd / "agent_search.db")
    assert classify_integrity(stdout, stderr, exit_code) == "A"
