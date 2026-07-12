"""restore-cass.sh **semantic 路由**集成 smoke（codex 2026-07-12 R9-[critical]）。

step 8 的最终门必须用**生产 cass-mcp 真依赖的** semantic 检索路由验证，否则验的是另一条路
（native/default semantic 可用 ≠ 用户依赖的 daemon+bge-m3+rerank 路由可用）——好恢复可能误判失败，
或没证明 cass-mcp semantic 可用就报成功。既有 integration 只跑 `--skip-semantic`（lexical 分支），
semantic 分支此前**零集成覆盖**。

这里跑**非 skip-semantic** 真脚本，stub 掉外部命令，让 `cass-infinity search` **录下真实 argv**，
断言其含 `cass_mcp.config.SEMANTIC_FLAGS`（单一事实源）的连续子序列——SEMANTIC_FLAGS 变了、
或脚本漂移，本测试即红。
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra" / "backup" / "restore-cass.sh"
sys.path.insert(0, str(REPO))
from cass_mcp.config import SEMANTIC_FLAGS  # noqa: E402  生产语义检索固定 flags（单一事实源）


def _write_stub(path: pathlib.Path, body: str):
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(0o755)


def _make_fake_backup(dest: pathlib.Path) -> str:
    """造一份最小合法备份（真 sha 一致 + digest 锚点自洽 + 共享 blob 池）。"""
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
    (bk / "digest.json").write_text(
        f'{{"db_sha256":"{db_sha}","manifests_sha256sum_sha256":"{man_sidecar_sha}"}}'
    )
    (bk / "COMPLETE").write_text("")
    (dest / "raw-mirror" / "v1" / "blobs" / "blake3" / "00").mkdir(parents=True)
    (dest / "raw-mirror" / "v1" / "blobs" / "blake3" / "00" / ("00" * 32 + ".raw")).write_bytes(b"blob")
    (dest / "sessions").mkdir()
    return bk.name


def _contains_subseq(hay: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return any(hay[i : i + n] == needle for i in range(len(hay) - n + 1))


def test_semantic_gate_uses_production_flags(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "share").mkdir(parents=True)
    dest = tmp_path / "backups"
    dest.mkdir()
    _make_fake_backup(dest)
    target = tmp_path / "restore-target"
    argv_file = tmp_path / "search.argv"  # cass-infinity search 把真实 argv 写这

    stub = tmp_path / "stub-bin"
    stub.mkdir()
    _write_stub(stub / "flock", "exit 0")
    _write_stub(stub / "systemctl", 'case "$*" in *"is-active"*) exit 1;; esac; exit 0')
    _write_stub(stub / "pgrep", "exit 1")
    _write_stub(
        stub / "uv",
        'case "$*" in *"import blake3"*) exit 0;; esac; shift 2; exec python3 "$@"',
    )
    # cass-infinity：index 造 >500KB；models backfill 一轮即 published + 落 semantic 产物；
    #                doctor 吐合法 JSON；search **录 argv** 再吐 hits。
    _write_stub(
        stub / "cass-infinity",
        (
            'case "$1" in\n'
            '  index) mkdir -p "$CASS_DATA_DIR/index"; head -c 700000 /dev/zero > "$CASS_DATA_DIR/index/seg"; exit 0;;\n'
            '  models) mkdir -p "$CASS_DATA_DIR/vector_index";\n'
            '          : > "$CASS_DATA_DIR/vector_index/index-bge-m3.fsvi";\n'
            '          echo \'{"quality_tier":{"ready":true,"embedder_id":"bge-m3"}}\' > "$CASS_DATA_DIR/vector_index/semantic_manifest.json";\n'
            '          echo \'{"published":true,"last_offset":1}\'; exit 0;;\n'
            '  doctor) echo \'{"raw_mirror":{"status":"verified","summary":{"missing_blob_count":0,"checksum_mismatch_count":0,"manifest_checksum_mismatch_count":0,"invalid_manifest_count":0,"interrupted_capture_count":0,"verified_blob_count":1}}}\'; exit 0;;\n'
            '  search) shift; printf \'%s\\n\' "$@" > "$SEARCH_ARGV_FILE"; echo \'{"hits":[{"id":1}]}\'; exit 0;;\n'
            '  *) exit 0;;\n'
            "esac"
        ),
    )
    _write_stub(stub / "rsync", "exit 0")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{stub}{os.pathsep}" + env["PATH"]
    env["CASS_BACKUP_DEST"] = str(dest)
    env["CASS_BIN"] = str(stub / "cass-infinity")
    env["SEARCH_ARGV_FILE"] = str(argv_file)

    p = subprocess.run(
        ["bash", str(SCRIPT), "--data-dir", str(target)],  # **不加** --skip-semantic → 走 semantic 分支
        capture_output=True, text=True, env=env, timeout=120,
    )
    out = p.stdout + p.stderr
    assert p.returncode == 0, f"semantic happy path 应 rc=0，实得 {p.returncode}:\n{out}"
    assert argv_file.exists(), f"cass-infinity search 未被调用（没走到 step 8 semantic 门）:\n{out}"

    recorded = argv_file.read_text().splitlines()
    assert _contains_subseq(recorded, list(SEMANTIC_FLAGS)), (
        "step 8 semantic 验证的 flags 与生产 cass_mcp.config.SEMANTIC_FLAGS 不一致——"
        f"验的是错路由（R9-critical）。\n期望连续含: {list(SEMANTIC_FLAGS)}\n实录 argv: {recorded}"
    )
