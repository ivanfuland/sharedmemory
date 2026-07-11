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


class PublishedScanError(Exception):
    """基线集扫描（strict）发现「含 COMPLETE 但 generation 不可读/非法」的 cass-*/
    成员——真实链 tip 不可信。基线选择绝不能像轮转那样宽容 skip 掉它、静默退回更老
    的一份比对（那会把相对真实上一份缩水的坏备份当好备份放行，codex R4-P0）。"""


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


def _scan_published(dest) -> tuple[list[tuple[int, str, dict]], list[str]]:
    """扫 `dest` 下含 `COMPLETE` 的 `cass-*/` 目录，各读 digest.json。返回
    `(valid, skipped)`：

    - `valid`：digest 可读且 `generation` 为 int 的成员 `(generation, 目录名,
      digest dict)` 列表（未排序）。
    - `skipped`：含 COMPLETE 但 digest/generation 读不到或非法的成员，每个一条
      人读原因（无 digest.json / 读失败 / 坏 JSON / 非 dict / 缺 generation 键 /
      generation 非 int）。**这是 `_iter_published` 宽容跳过、`latest_published(
      strict=True)` 响亮拒绝的同一批成员**——两层共用这一份分类逻辑（DRY，避免
      skip 条件在两处漂移，codex R4-P0「挂一漏万」教训）。

    失败语义分两层，不可混（历史注释保留在此处，是本模块唯一的 skip 判据源头）：

    - **目录探测层（is_dir / COMPLETE 存在性）刻意不包 try**：OS 级错误（如
      `chmod 000` 导致的 `PermissionError`，Python 3.12 起 `Path.exists()` 不再
      吞它）**照常上抛 = 响亮失败**。DEST 子目录整体权限坏属于完整性事件，若
      静默跳过会把它变成「首晚模式」（无前驱），绕过 generation 链比对发布假绿；
      上抛则消费方（五腿门 CLI / 轮转选点）当场崩、备份当晚 FATAL，人必须来看。
    - **digest 内容层**：无 digest.json / 读失败 / 坏 JSON / 非 dict / 缺
      `generation` 键 / `generation` 非 int——`_iter_published`（轮转）宽容跳过
      （少删安全）；`latest_published(strict=True)`（基线选择）拿 `skipped` 非空
      即 raise（选错 tip 危险，见 `PublishedScanError`）。
    """
    dest = pathlib.Path(dest)
    valid: list[tuple[int, str, dict]] = []
    skipped: list[str] = []
    for entry in sorted(dest.glob("cass-*")):
        if not entry.is_dir():
            continue
        if not (entry / "COMPLETE").exists():
            continue
        try:
            digest = read_digest(entry)
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append(f"{entry.name}: digest.json 读取失败（{type(exc).__name__}: {exc}）")
            continue
        if not isinstance(digest, dict) or "generation" not in digest:
            # 非 dict（合法 JSON 但裸标量，如 digest.json 内容是 `5`/`true`）同样归
            # 入「读不到 generation」——不然 `"generation" not in digest` 对 int/bool
            # 会 TypeError（`in` 要求可迭代）。digest is None（无 digest.json）也在此。
            skipped.append(f"{entry.name}: digest.json 缺失/非 dict/缺 generation 键")
            continue
        if type(digest["generation"]) is not int:
            # 脏数据（如手编 digest 把 generation 写成字符串 "7"）——不 crash（下游
            # sorted/max 混型比较会 TypeError），也绝不据此删目录。type() 精确匹配
            # 顺带排除 bool。
            skipped.append(f"{entry.name}: generation 非 int（{digest['generation']!r}）")
            continue
        valid.append((digest["generation"], entry.name, digest))
    return valid, skipped


def _iter_published(dest) -> list[tuple[int, str, dict]]:
    """轮转/宽容视角：只返回 `_scan_published` 的 valid 成员（读不到 generation 的
    目录静默跳过——`rotation_victims` 的「少删安全」语义）。基线选择请改用
    `latest_published(dest, strict=True)`（strict 拒绝跳过、绝不选错 tip）。"""
    return _scan_published(dest)[0]


def latest_published(dest, *, strict: bool = False) -> tuple[str, dict] | None:
    """在 `dest` 下扫含 `COMPLETE` 的 `cass-*/` 目录，各读 digest.json，返回
    `generation` 最大者的 `(目录名, digest dict)`；一个都没有返回 None。不看 mtime。

    - `strict=False`（默认，历史行为）：读不到 digest/generation 的目录**宽容跳过**。
    - `strict=True`（基线选择专用，codex R4-P0）：任何一个含 COMPLETE 的 cass-*/
      的 generation 不可读/非法 ⇒ raise `PublishedScanError`。真实链 tip 不可读 =
      基线集不可信，绝不静默退回更老的一份比对（那会把相对真实上一份缩水的坏备份
      当好备份放行）。
    """
    valid, skipped = _scan_published(dest)
    if strict and skipped:
        raise PublishedScanError(
            "基线集不可信（含 COMPLETE 但 generation 不可读/非法的成员，绝不静默退回"
            "更老基线；需人工调查/rebaseline）: " + "; ".join(skipped)
        )
    if not valid:
        return None
    generation, name, digest = max(valid, key=lambda t: t[0])
    return (name, digest)


def rotation_victims(dest, keep: int) -> list[str]:
    """keep-N 轮转选点（spec §6 step 16/17、§7、§11）：在 `dest` 下扫含 `COMPLETE`
    的 `cass-*/` 目录，按各自 digest.json 的 `generation` 升序排序，返回超出
    `keep` 个之后、最旧的那些目录名（待删）。不看 mtime——`touch`/`cp -a`/restore
    演练都会改写它。读不到 `generation` 的目录（无 digest.json / 坏 JSON / 缺键 /
    非 int）不参与排序也不出现在返回值里（`_iter_published` 已经把它们筛掉）；
    目录探测层的 OS 级错误照常上抛（见 `_iter_published` 的两层语义），调用方
    （backup-cass.sh 的选点段）以 rc 非零接住并置 `ROTATE_FAIL`。候选总数
    未超过 `keep` 时返回空列表。"""
    published = sorted(_iter_published(dest), key=lambda t: t[0])
    if len(published) <= keep:
        return []
    n_victims = len(published) - keep
    return [name for _generation, name, _digest in published[:n_victims]]


def pre_reset_victims(dest, reset_generation: int) -> list[str]:
    """retention_reset 轮转选点（spec §8.3，codex R5-P1）：返回所有 `generation <
    reset_generation` 的含 `COMPLETE` 的 `cass-*/` 目录名——retention_reset 是重置
    点，其**前**的备份全部轮转掉，使重置点成为 R 中链头（spec §8.3「带它的那份必须
    是链头」+「早于 r 的备份允许不在 R——它们就是被重置掉的」）。这与 keep-N 常规
    轮转不同：不留 keep 个，重置点之前一律清空。

    宽容 skip 语义同 `rotation_victims`（读不到 `generation` 的目录不参与、不被删，
    `_iter_published` 已筛掉）；目录探测层 OS 级错误照常上抛。保护集
    （SUSPECT-*/INCOMPLETE-*/RECOVERABLE-*/raw-mirror/sessions/state/pre-franken）
    天然不匹配「含 COMPLETE 的 cass-*/」，不在候选集里。`reset_generation` 是本次
    发布（重置点）的 generation——它自身 `gen == reset_generation` 不 `< `，绝不
    被选为 victim。"""
    return [
        name for gen, name, _digest in _iter_published(dest) if gen < reset_generation
    ]


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
