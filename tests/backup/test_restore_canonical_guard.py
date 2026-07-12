"""restore-cass.sh **canonical 目标 fail-closed guard**（codex 2026-07-12 R10-[critical]）。

设计强制 staging + swap：restore 落到 `<canonical>.new` 之类的**新目录**，验证全过后人工 swap
（spec V25「零生产改动」）。若操作者把 `--data-dir` 直接指向 live canonical（cass-mcp 生产库），
step3 之后任何 FATAL 会走 cleanup **重启 cass-mcp**，而服务 env 仍指向 canonical → 读到**半恢复**
生产库。故 `--data-dir` 解析后等于 live canonical 必须 **fail-fast**（在停任何服务之前）。
"""
from __future__ import annotations

import os
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra" / "backup" / "restore-cass.sh"


def _write_stub(path: pathlib.Path, body: str):
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(0o755)


def test_rejects_data_dir_equal_to_canonical(tmp_path):
    canonical = tmp_path / "coding-agent-search"
    canonical.mkdir()  # 空 canonical（刚重建/被清）——非空门放行，必须由 canonical guard 兜住
    systemctl_log = tmp_path / "systemctl.calls"

    stub = tmp_path / "stub-bin"
    stub.mkdir()
    _write_stub(stub / "flock", "exit 0")
    # systemctl：记录每次调用；guard 若生效，绝不该出现 stop
    _write_stub(
        stub / "systemctl",
        'printf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"; case "$*" in *"is-active"*) exit 1;; esac; exit 0',
    )
    _write_stub(stub / "pgrep", "exit 1")

    empty_dest = tmp_path / "backups"  # 空备份池：未加 guard 时脚本在 backup 解析处快速 FATAL（非 canonical 消息）
    empty_dest.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{stub}{os.pathsep}" + env["PATH"]
    env["CASS_CANONICAL_DIR"] = str(canonical)
    env["CASS_BACKUP_DEST"] = str(empty_dest)
    env["SYSTEMCTL_LOG"] = str(systemctl_log)

    p = subprocess.run(
        ["bash", str(SCRIPT), "--data-dir", str(canonical)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    out = p.stdout + p.stderr
    assert p.returncode != 0, f"--data-dir==canonical 必须被拒，实得 rc=0:\n{out}"
    assert "canonical" in out, f"错误信息应点明 canonical，实得:\n{out}"
    calls = systemctl_log.read_text() if systemctl_log.exists() else ""
    assert "stop" not in calls, f"guard 必须在停服务**之前** fail-fast，但 systemctl stop 已被调用:\n{calls}"
