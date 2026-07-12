"""restore-cass.sh **会话源恢复 fail-closed 门** `restore_sessions_check.py` 单测
（codex 2026-07-12 R10-[critical]）。

会话源恢复（`--sessions-into[-source]`）此前只从共享池 rsync、**不校验所选备份**：池缺失/腐烂/
少文件时 DB+索引 doctor/search 仍 PASS，脚本**谎报成功**（源全丢场景下生产会话源保持空缺）。

本门在复制**之前**校验（fail-closed，缺一即 FATAL）：
  - `sha256(sessions.tsv) == digest.sessions_tsv_sha256`（所选备份的 sessions 清单自洽，probe 实测恒等）；
  - 清单每一行（`<relpath>\t<size>\t<blake3>\t<status>`，跳过 `#`/空行）对应的 `<pool_root>/<relpath>`
    **存在 + size 相符 + blake3 相符**（probe 实测 tsv 的 blake3 = pool 文件内容 blake3）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib

import blake3  # restore 硬依赖（preflight 已校验）
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
_MOD_PATH = REPO / "infra" / "backup" / "cass" / "restore_sessions_check.py"
_spec = importlib.util.spec_from_file_location("restore_sessions_check", _MOD_PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _b3(data: bytes) -> str:
    return blake3.blake3(data).hexdigest()


def _build(tmp: pathlib.Path, rows):
    """rows: list of (relpath, content_bytes, status)。造 pool + sessions.tsv + digest 值。"""
    pool = tmp / "sessions"
    lines = []
    for rel, content, status in rows:
        f = pool / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(content)
        lines.append(f"{rel}\t{len(content)}\t{_b3(content)}\t{status}")
    body = "\n".join(lines) + "\n"
    tsv = tmp / "sessions.tsv"
    # #sha256 首行（真格式有此自校验行，门应跳过）；digest 锚点 = 整文件 sha256（probe 实测）
    tsv.write_text(f"#sha256 {hashlib.sha256(body.encode()).hexdigest()}\n" + body)
    digest = hashlib.sha256(tsv.read_bytes()).hexdigest()
    return str(tsv), digest, str(pool)


def test_consistent_passes(tmp_path):
    tsv, digest, pool = _build(tmp_path, [
        ("claude-projects/-home-ivan-x/a.jsonl", b"session a bytes", "present"),
        ("codex-sessions/sess/b.jsonl", b"another session", "present"),
    ])
    out = mod.check(tsv, digest, pool)
    assert "2" in out  # 校验了 2 条


def test_digest_mismatch_fails(tmp_path):
    tsv, _digest, pool = _build(tmp_path, [("claude-projects/x/a.jsonl", b"a", "present")])
    with pytest.raises(SystemExit) as e:
        mod.check(tsv, "deadbeef" * 8, pool)  # 错的 digest
    assert "sessions.tsv" in str(e.value) and "digest" in str(e.value).lower()


def test_pool_missing_file_fails(tmp_path):
    tsv, digest, pool = _build(tmp_path, [
        ("claude-projects/x/a.jsonl", b"a", "present"),
        ("claude-projects/x/gone.jsonl", b"will delete", "present"),
    ])
    (pathlib.Path(pool) / "claude-projects/x/gone.jsonl").unlink()  # 池缺一个
    with pytest.raises(SystemExit) as e:
        mod.check(tsv, digest, pool)
    assert "gone.jsonl" in str(e.value)


def test_pool_size_mismatch_fails(tmp_path):
    tsv, digest, pool = _build(tmp_path, [("claude-projects/x/a.jsonl", b"original", "present")])
    (pathlib.Path(pool) / "claude-projects/x/a.jsonl").write_bytes(b"TRUNCATED")  # 改 size
    with pytest.raises(SystemExit) as e:
        mod.check(tsv, digest, pool)
    assert "a.jsonl" in str(e.value)


def test_pool_blake3_mismatch_fails(tmp_path):
    tsv, digest, pool = _build(tmp_path, [("claude-projects/x/a.jsonl", b"12345678", "present")])
    # 同 size、不同内容 → blake3 变（size 门放行、blake3 门必须兜住腐烂）
    (pathlib.Path(pool) / "claude-projects/x/a.jsonl").write_bytes(b"ABCDEFGH")
    with pytest.raises(SystemExit) as e:
        mod.check(tsv, digest, pool)
    assert "blake3" in str(e.value).lower() or "a.jsonl" in str(e.value)
