"""restore-cass.sh 的 **bash 集成 smoke**（codex 2026-07-12 R7 要求）。

helper 单测（doctor门/cleanup/manifest）测不到「shell 集成路径本身」——例如 R6 引入的
`$_MAN_N` unbound（set -u 下 step2 之后必崩、happy path 走不完）就是被这类测试漏掉的。
这里 stub 掉外部命令（systemctl/flock/pgrep/uv/cass-infinity/rsync），用**真文件 + 真校验逻辑**
造一份最小合法备份，`--skip-semantic` 跑**真脚本**到收尾，断言：
  - 退出码 0（happy path 走完，无 unbound-var / 控制流早退）；
  - stdout 出现 step2 OK 与"全部步骤完成"；
  - 目标 data-dir 落了 agent_search.db 与 raw-mirror。
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra" / "backup" / "restore-cass.sh"


def _write_stub(path: pathlib.Path, body: str):
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(0o755)


def _make_fake_backup(dest: pathlib.Path) -> str:
    """造一份最小合法备份 cass-<stamp>/（真 sha 一致 + digest 锚点自洽 + 共享 blob 池）。"""
    bk = dest / "cass-20260101-000000-1"
    (bk / "manifests").mkdir(parents=True)
    # db
    (bk / "db").write_bytes(b"SQLite fake db bytes")
    db_sha = hashlib.sha256((bk / "db").read_bytes()).hexdigest()
    (bk / "db.sha256").write_text(f"{db_sha}  db\n")
    # 1 个 manifest + sidecar（路径 manifests/<name>，相对备份根）
    man = bk / "manifests" / "m1.json"
    man.write_text('{"blob_blake3":"00"}')
    man_sha = hashlib.sha256(man.read_bytes()).hexdigest()
    (bk / "manifests.sha256sum").write_text(f"{man_sha}  manifests/m1.json\n")
    man_sidecar_sha = hashlib.sha256((bk / "manifests.sha256sum").read_bytes()).hexdigest()
    # digest 锚点自洽
    (bk / "digest.json").write_text(
        f'{{"db_sha256":"{db_sha}","manifests_sha256sum_sha256":"{man_sidecar_sha}"}}'
    )
    (bk / "census.tsv").write_text("messages\t1\n")
    (bk / "sessions.tsv").write_text("")
    (bk / "COMPLETE").write_text("")
    # 共享 blob 池 + sessions 通道（restore step4 从这取）
    (dest / "raw-mirror" / "v1" / "blobs" / "blake3" / "00").mkdir(parents=True)
    (dest / "raw-mirror" / "v1" / "blobs" / "blake3" / "00" / ("00" * 32 + ".raw")).write_bytes(b"blob")
    (dest / "sessions").mkdir()
    return bk.name


def test_happy_path_skip_semantic_completes(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "share").mkdir(parents=True)
    dest = tmp_path / "backups"
    dest.mkdir()
    _make_fake_backup(dest)
    target = tmp_path / "restore-target"  # 不预建（脚本 mkdir）

    stub = tmp_path / "stub-bin"
    stub.mkdir()
    # flock：总成功（不真锁）
    _write_stub(stub / "flock", 'exit 0')
    # systemctl：is-active 返回 inactive（→ _MCP_WAS_ACTIVE=0，cleanup 不重启）；stop/start 成功
    _write_stub(stub / "systemctl", 'case "$*" in *"is-active"*) exit 1;; esac; exit 0')
    _write_stub(stub / "pgrep", 'exit 1')  # 无 cass-infinity
    # uv：`uv run python ...`；blake3 检查放行，其余转真 python3（跑真 restore_verify/manifest_check）
    _write_stub(
        stub / "uv",
        'case "$*" in *"import blake3"*) exit 0;; esac; shift 2; exec python3 "$@"',
    )
    # cass-infinity：index --force-rebuild 造个 >500KB 的 index/；doctor 吐合法 JSON；search 吐 hits
    _write_stub(
        stub / "cass-infinity",
        (
            'case "$1" in\n'
            '  index) mkdir -p "$CASS_DATA_DIR/index"; head -c 700000 /dev/zero > "$CASS_DATA_DIR/index/seg"; exit 0;;\n'
            '  doctor) echo \'{"raw_mirror":{"status":"verified","summary":{"missing_blob_count":0,"checksum_mismatch_count":0,"manifest_checksum_mismatch_count":0,"invalid_manifest_count":0,"interrupted_capture_count":0,"verified_blob_count":1}}}\'; exit 0;;\n'
            '  search) echo \'{"hits":[{"id":1}]}\'; exit 0;;\n'
            '  *) exit 0;;\n'
            'esac'
        ),
    )
    _write_stub(stub / "rsync", 'exit 0')

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{stub}{os.pathsep}" + env["PATH"]
    env["CASS_BACKUP_DEST"] = str(dest)
    env["CASS_BIN"] = str(stub / "cass-infinity")

    p = subprocess.run(
        ["bash", str(SCRIPT), "--data-dir", str(target), "--skip-semantic"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    out = p.stdout + p.stderr
    assert "unbound variable" not in out, f"set -u unbound-var 崩（happy path 走不完）:\n{out}"
    assert p.returncode == 0, f"happy path 应 rc=0，实得 {p.returncode}:\n{out}"
    assert "step 2 OK" in out, out
    assert "全部步骤完成" in out, out
    assert (target / "agent_search.db").is_file()
    assert (target / "raw-mirror" / "v1" / "manifests" / "m1.json").is_file()
