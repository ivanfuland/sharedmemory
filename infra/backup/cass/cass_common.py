"""CASS 备份 PR1 共享基础件：canonical JSON 编码、流式 sha256/blake3（含 fadvise）、
digest.json 读取、按 generation 选链 tip、sessions state 文件的原子读写。

`infra/backup/cass/` 不是 package——同目录模块互相 import 的约定是在模块顶部
`sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` 后直接 `import cass_common`。

PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 偏好 / 基建拓扑 / 真实会话内容。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import signal
from collections import namedtuple

import blake3

_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class StateCorrupt(Exception):
    """sessions state 文件（如 sessions.state.tsv）的 `#sha256` 完整性头与实际内容
    字节不符，或缺失该首行 / 首行格式不对。"""


# status ∈ {"present", "absent_at_source"}
SessionRec = namedtuple("SessionRec", "relpath nas_size blake3 status")


def dumps_canonical(obj: dict) -> bytes:
    """确定性序列化：sort_keys + 紧凑分隔符 + 不转义非 ASCII。

    用于 digest.json：其字节参与链哈希，任何重排/重格式化都会破链——序列化必须确定性。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _stream_hash(path, hasher, fadvise: bool = False, prefix_len: int | None = None) -> str:
    """流式（1 MiB 块）更新 hasher 并返回 hexdigest。fadvise=True 时在 open 后立刻对整个
    文件调用一次 POSIX_FADV_DONTNEED（不占页缓存）。prefix_len 非 None 时只读前
    prefix_len 字节。"""
    remaining = prefix_len
    with open(path, "rb") as f:
        if fadvise:
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        while remaining is None or remaining > 0:
            read_size = _CHUNK_SIZE if remaining is None else min(_CHUNK_SIZE, remaining)
            chunk = f.read(read_size)
            if not chunk:
                break
            hasher.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return hasher.hexdigest()


def sha256_file(path, fadvise: bool = False) -> str:
    return _stream_hash(path, hashlib.sha256(), fadvise=fadvise)


def blake3_file(path, fadvise: bool = False, prefix_len: int | None = None) -> str:
    return _stream_hash(path, blake3.blake3(), fadvise=fadvise, prefix_len=prefix_len)


def read_digest(backup_dir) -> dict | None:
    """读 `<backup_dir>/digest.json`。不存在返回 None；存在则字节级读取后 json.loads
    （解析失败原样 raise，绝不回写/修复）。"""
    digest_path = pathlib.Path(backup_dir) / "digest.json"
    if not digest_path.exists():
        return None
    raw = digest_path.read_bytes()
    return json.loads(raw)


def latest_published(dest) -> tuple[str, dict] | None:
    """在 `dest` 下扫含 `COMPLETE` 的 `cass-*/` 目录，各读 digest.json，返回
    `generation` 最大者的 `(目录名, digest dict)`。不看 mtime；读不到 digest/generation
    的目录跳过；一个都没有返回 None。"""
    dest = pathlib.Path(dest)
    best: tuple[int, str, dict] | None = None
    for entry in sorted(dest.glob("cass-*")):
        if not entry.is_dir():
            continue
        if not (entry / "COMPLETE").exists():
            continue
        try:
            digest = read_digest(entry)
        except (OSError, json.JSONDecodeError):
            continue
        if not digest or "generation" not in digest:
            continue
        generation = digest["generation"]
        if best is None or generation > best[0]:
            best = (generation, entry.name, digest)
    if best is None:
        return None
    return (best[1], best[2])


def state_read(path) -> list[SessionRec]:
    """校验首行 `#sha256 <其余全部字节的 sha256 十六进制>`；不符则 raise StateCorrupt。"""
    path = pathlib.Path(path)
    raw = path.read_bytes()
    try:
        header, body = raw.split(b"\n", 1)
    except ValueError:
        raise StateCorrupt(f"{path}: state file has no header line")
    prefix = b"#sha256 "
    if not header.startswith(prefix):
        raise StateCorrupt(f"{path}: missing '#sha256' header")
    expected = header[len(prefix):].decode("ascii", "replace").strip()
    actual = hashlib.sha256(body).hexdigest()
    if expected != actual:
        raise StateCorrupt(
            f"{path}: checksum mismatch (header {expected!r} != computed {actual!r})"
        )
    records: list[SessionRec] = []
    for line in body.decode("utf-8").split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise StateCorrupt(f"{path}: malformed record line: {line!r}")
        relpath, nas_size, blake3_hex, status = parts
        try:
            nas_size = int(nas_size)
        except ValueError:
            raise StateCorrupt(f"{path}: non-integer nas_size in line: {line!r}")
        records.append(SessionRec(relpath, nas_size, blake3_hex, status))
    return records


def state_write_atomic(path, records, *, _kill_before_replace: bool = False) -> None:
    """生成 `#sha256 ...` 首行 + 记录行，写同目录 `.tmp` 后 `os.replace`（单文件原子）。

    `_kill_before_replace`：Task 12 DEV-7 故障注入专用旋钮（`CASS_BACKUP_FAULT=
    kill-before-state-publish`）——`.tmp` 落盘后、`os.replace` 前自杀（`SIGKILL`，
    不可捕获），验证「旧 state 原封不动、下一轮正常运行」的单文件原子性
    （V12n）。默认 `False`，对本函数其余全部既有调用方（含测试直接构造 fixture）
    零行为变化。调用方（`cass_sessions.py`）负责读取 `CASS_BACKUP_FAULT` env 并据此
    决定是否传 `True`——本模块不认识任何 FAULT 名字。"""
    path = pathlib.Path(path)
    body = "".join(
        f"{r.relpath}\t{r.nas_size}\t{r.blake3}\t{r.status}\n" for r in records
    ).encode("utf-8")
    header = f"#sha256 {hashlib.sha256(body).hexdigest()}\n".encode("ascii")
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_bytes(header + body)
    if _kill_before_replace:
        os.kill(os.getpid(), signal.SIGKILL)
    os.replace(tmp_path, path)
