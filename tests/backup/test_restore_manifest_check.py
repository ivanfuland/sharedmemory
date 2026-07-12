"""restore-cass.sh step 2 的 manifest 精确快照门（restore_manifest_check.py）单测。

覆盖 codex R6-[critical] 的绕过形态：只比数量不够——symlink 逃过 find -type f、重复 sidecar 行 +
漏列另一个都是"数量相等集合不等"。这里逐项集合比较 + 拒 symlink + 拒重复。
"""
from __future__ import annotations

import hashlib
import pathlib

import pytest

from restore_manifest_check import check


def _mk(tmp_path: pathlib.Path, names: list[str], sidecar_lines: list[str] | None = None):
    """建 manifests/ 目录 + 若干真 .json 文件；sidecar_lines 缺省用 names 生成正确 sidecar。"""
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    for n in names:
        (mdir / n).write_text('{"x":1}', encoding="utf-8")
    sidecar = tmp_path / "manifests.sha256sum"
    if sidecar_lines is None:
        sidecar_lines = [
            f"{hashlib.sha256((mdir / n).read_bytes()).hexdigest()}  manifests/{n}" for n in names
        ]
    sidecar.write_text("\n".join(sidecar_lines) + "\n", encoding="utf-8")
    return str(sidecar), str(mdir)


def test_exact_set_passes(tmp_path):
    sidecar, mdir = _mk(tmp_path, ["a.json", "b.json"])
    msg = check(sidecar, mdir)
    assert "集合恒等" in msg


def test_extra_regular_file_not_in_sidecar_fails(tmp_path):
    sidecar, mdir = _mk(tmp_path, ["a.json"])
    (pathlib.Path(mdir) / "sneaky.json").write_text("{}", encoding="utf-8")  # 多一个未列入
    with pytest.raises(SystemExit) as ei:
        check(sidecar, mdir)
    assert "sneaky.json" in str(ei.value) or "不符" in str(ei.value)


def test_symlink_json_rejected_even_if_count_matches(tmp_path):
    # 关键：sidecar 列 a.json（1 个），目录里 a.json 是真文件 + b.json 是 symlink（find -type f 只数到 1，
    # 数量看似相等），但 symlink 必须被拒（cp -a 会复制它、reader 会读它 → 非精确快照）
    sidecar, mdir = _mk(tmp_path, ["a.json"])
    target = pathlib.Path(mdir) / "a.json"
    (pathlib.Path(mdir) / "b.json").symlink_to(target)
    with pytest.raises(SystemExit) as ei:
        check(sidecar, mdir)
    assert "symlink" in str(ei.value)


def test_duplicate_sidecar_path_fails(tmp_path):
    # sidecar 有重复 relpath（数量虚高的伪装）
    sidecar, mdir = _mk(
        tmp_path,
        ["a.json", "b.json"],
        sidecar_lines=[
            "aa  manifests/a.json",
            "aa  manifests/a.json",  # 重复
            "bb  manifests/b.json",
        ],
    )
    with pytest.raises(SystemExit) as ei:
        check(sidecar, mdir)
    assert "重复" in str(ei.value)


def test_missing_file_listed_in_sidecar_fails(tmp_path):
    # sidecar 列 a.json + c.json，但目录只有 a.json（缺 c.json）
    sidecar, mdir = _mk(
        tmp_path,
        ["a.json"],
        sidecar_lines=["aa  manifests/a.json", "cc  manifests/c.json"],
    )
    with pytest.raises(SystemExit) as ei:
        check(sidecar, mdir)
    assert "不符" in str(ei.value) or "c.json" in str(ei.value)


def test_empty_sidecar_fails(tmp_path):
    sidecar, mdir = _mk(tmp_path, ["a.json"], sidecar_lines=[])
    with pytest.raises(SystemExit) as ei:
        check(sidecar, mdir)
    assert "为空" in str(ei.value)
