"""restore-cass.sh 会话源恢复 **fail-closed 集成 smoke**（codex 2026-07-12 R10-[critical]）。

跑真脚本走 `--sessions-into <staging>` 分支：
  - **fail-closed**：所选备份 sessions.tsv 列了某文件、但 NAS 池缺它 → 必须 **FATAL**（不再 rsync 完
    残缺集合还报成功）；
  - **happy**：池与清单一致 → 恢复成功，jsonl 真落到 staging。

uv stub 必须 exec **venv python**（会话门 import blake3，系统 python3 无此模块）。
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import sys

import blake3

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra" / "backup" / "restore-cass.sh"


def _b3(data: bytes) -> str:
    return blake3.blake3(data).hexdigest()


def _write_stub(path: pathlib.Path, body: str):
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(0o755)


def _make_backup_with_sessions(dest: pathlib.Path, session_rows):
    """最小合法备份 + sessions.tsv/digest 锚点 + 共享 sessions 池。session_rows: [(relpath, bytes)]。"""
    bk = dest / "cass-20260101-000000-1"
    (bk / "manifests").mkdir(parents=True)
    (bk / "db").write_bytes(b"SQLite fake db bytes")
    db_sha = hashlib.sha256((bk / "db").read_bytes()).hexdigest()
    (bk / "db.sha256").write_text(f"{db_sha}  db\n")
    man = bk / "manifests" / "m1.json"
    man.write_text('{"blob_blake3":"00"}')
    man_sha = hashlib.sha256(man.read_bytes()).hexdigest()
    (bk / "manifests.sha256sum").write_text(f"{man_sha}  manifests/m1.json\n")
    man_sidecar_sha = hashlib.sha256((bk / "manifests.sha256sum").read_bytes()).hexdigest()
    # sessions.tsv + 共享池
    pool = dest / "sessions"
    lines = []
    for rel, content in session_rows:
        f = pool / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(content)
        lines.append(f"{rel}\t{len(content)}\t{_b3(content)}\tpresent")
    body = "\n".join(lines) + "\n"
    (bk / "sessions.tsv").write_text(f"#sha256 {hashlib.sha256(body.encode()).hexdigest()}\n" + body)
    sess_sha = hashlib.sha256((bk / "sessions.tsv").read_bytes()).hexdigest()
    # digest 锚点（含 sessions_tsv_sha256）
    (bk / "digest.json").write_text(
        f'{{"db_sha256":"{db_sha}","manifests_sha256sum_sha256":"{man_sidecar_sha}",'
        f'"sessions_tsv_sha256":"{sess_sha}"}}'
    )
    (bk / "COMPLETE").write_text("")
    (dest / "raw-mirror" / "v1" / "blobs" / "blake3" / "00").mkdir(parents=True)
    (dest / "raw-mirror" / "v1" / "blobs" / "blake3" / "00" / ("00" * 32 + ".raw")).write_bytes(b"blob")
    return bk


def _common_env_and_stubs(tmp_path):
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    _write_stub(stub / "flock", "exit 0")
    _write_stub(stub / "systemctl", 'case "$*" in *"is-active"*) exit 1;; esac; exit 0')
    _write_stub(stub / "pgrep", "exit 1")
    # uv：blake3 preflight 放行；其余 exec **venv python**（会话门需 blake3）
    _write_stub(stub / "uv", 'case "$*" in *"import blake3"*) exit 0;; esac; shift 2; exec "$GATE_PY" "$@"')
    _write_stub(
        stub / "cass-infinity",
        (
            'case "$1" in\n'
            '  index) mkdir -p "$CASS_DATA_DIR/index"; head -c 700000 /dev/zero > "$CASS_DATA_DIR/index/seg"; exit 0;;\n'
            '  doctor) echo \'{"raw_mirror":{"status":"verified","summary":{"missing_blob_count":0,"checksum_mismatch_count":0,"manifest_checksum_mismatch_count":0,"invalid_manifest_count":0,"interrupted_capture_count":0,"verified_blob_count":1}}}\'; exit 0;;\n'
            '  search) echo \'{"hits":[{"id":1}]}\'; exit 0;;\n'
            '  *) exit 0;;\n'
            "esac"
        ),
    )
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home" / ".local" / "share").mkdir(parents=True)
    env["PATH"] = f"{stub}{os.pathsep}" + env["PATH"]
    env["GATE_PY"] = sys.executable  # venv python（含 blake3）
    env["CASS_CANONICAL_DIR"] = str(tmp_path / "canonical")  # ≠ target，避免 F2 guard 误触
    return env


def test_sessions_pool_incomplete_fails_closed(tmp_path):
    dest = tmp_path / "backups"; dest.mkdir()
    _make_backup_with_sessions(dest, [
        ("claude-projects/proj/a.jsonl", b"session a"),
        ("claude-projects/proj/gone.jsonl", b"listed but will be deleted from pool"),
    ])
    (dest / "sessions" / "claude-projects" / "proj" / "gone.jsonl").unlink()  # 池缺一个（清单仍列它）
    staging = tmp_path / "staging"
    target = tmp_path / "restore-target"

    env = _common_env_and_stubs(tmp_path)
    env["CASS_BACKUP_DEST"] = str(dest)
    env["CASS_BIN"] = str(tmp_path / "stub-bin" / "cass-infinity")

    p = subprocess.run(
        ["bash", str(SCRIPT), "--data-dir", str(target), "--skip-semantic", "--sessions-into", str(staging)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    out = p.stdout + p.stderr
    assert p.returncode != 0, f"池缺文件必须 fail-closed，实得 rc=0（谎报成功）:\n{out}"
    assert "全部步骤完成" not in out, f"不该在会话池不完整时跑到收尾:\n{out}"
    assert "gone.jsonl" in out, f"错误应点名缺失文件:\n{out}"


def test_sessions_complete_recovers_to_staging(tmp_path):
    dest = tmp_path / "backups"; dest.mkdir()
    _make_backup_with_sessions(dest, [
        ("claude-projects/proj/a.jsonl", b"session a content"),
        ("codex-sessions/s/b.jsonl", b"session b content"),
    ])
    staging = tmp_path / "staging"
    target = tmp_path / "restore-target"

    env = _common_env_and_stubs(tmp_path)
    env["CASS_BACKUP_DEST"] = str(dest)
    env["CASS_BIN"] = str(tmp_path / "stub-bin" / "cass-infinity")

    p = subprocess.run(
        ["bash", str(SCRIPT), "--data-dir", str(target), "--skip-semantic", "--sessions-into", str(staging)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    out = p.stdout + p.stderr
    assert p.returncode == 0, f"池完整应成功，实得 rc={p.returncode}:\n{out}"
    assert "全部步骤完成" in out, out
    # 真落到 staging（--sessions-into <staging>/<alias>/<relpath-minus-alias>）
    assert (staging / "claude-projects" / "proj" / "a.jsonl").is_file(), "会话 a 未落 staging"
    assert (staging / "codex-sessions" / "s" / "b.jsonl").is_file(), "会话 b 未落 staging"
