# everos_mcp/blobstore.py
"""内容寻址 blobstore + 双快照(payload/passage)组装。

规则(见任务简报,均为审查阻断项):
- Canonical 序列化:dict -> `json.dumps(obj, ensure_ascii=False, sort_keys=True)`,
  str 原样存;统一 UTF-8 编码后取 sha256 作为文件名(`blobs/<sha256>`)。
- 写协议:唯一 tmp 名(pid+uuid)写入+fsync -> 若目标已存在,读回校验 hash
  是否与文件名一致(不符 raise `BlobCorruption`,吻合则视为幂等去重,丢弃
  tmp)-> 否则 rename 到目标 + 目录 fsync(保证 rename 落盘)。
- 读时重算 hash,不符 raise `BlobCorruption`(篡改/损坏检测,不信任文件名)。
- 文件 0600、目录 0700(账目录下全体的通行铁律)。
- `build_snapshots` 是纯函数,不持有 BlobStore 实例:payload 侧走
  `everos_mcp.contract.clamp_payload`(cap 用其默认值 8000,与 passage 侧的
  token cap 语义不同);passage 侧必须调用
  `everos_eval.probe_passage.build_passage`(打分锚口径与探针冻结规格
  同源,禁止在本模块复制截断逻辑)。两侧的 sha 用与 BlobStore.put 完全相同
  的 canonical 序列化算出,保证调用方后续 `store.put(snap.payload_clamped)`
  / `store.put(snap.passage_text)` 得到的 sha 与 Snapshot 里预先算好的
  `payload_sha`/`passage_sha` 逐位相同(内容寻址的一致性来自"同一套哈希
  规则",不是来自共享一个 store 实例)。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from everos_mcp import contract
from everos_eval import probe_passage

_DIR_MODE = 0o700
_FILE_MODE = 0o600

# M3.1:sha 必须是 64 位小写十六进制(sha256 hexdigest 的形状),否则一律拒绝
# ——不允许把 `sha` 拼进 `blobs_dir / sha` 之前先做这道格式校验,traversal-shaped
# 输入(`"../../x"`、`"/etc/hostname"` 等)必须在**触碰文件系统之前**被拒。
_SHA_RE = re.compile(r"[0-9a-f]{64}")


class BlobCorruption(Exception):
    """磁盘上的 blob 内容与其文件名(声称的 sha256)不一致——篡改或损坏。

    `get()`/`exists()` 对非法 sha 格式(长度不对/含路径分隔符/大写等)也复用
    这个异常类——traversal-shaped 输入本质上就是"文件名与内容寻址约定不符"
    的一种,判给同一异常比新开一个更细的异常类更省心,调用方(scorer 等)本来
    就已经把这个异常当"这份内容不可信"处理。`exists()` 与 `get()` 采用相同的
    fail-closed 语义(拒绝而非返回 False),避免两个方法对同一类非法输入给出
    不一致的信号。"""


def canonical_bytes(obj: dict | str) -> bytes:
    """dict -> `json.dumps(obj, ensure_ascii=False, sort_keys=True)`;str 原样。
    统一 UTF-8 编码。BlobStore.put 与 build_snapshots 共用这一份规则,保证
    两处独立算出的 sha256 永远吻合。"""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _assert_valid_sha(sha: str) -> None:
    """M3.1:`get()`/`exists()` 的公共入口校验——`sha` 必须精确匹配
    `[0-9a-f]{64}`,不符一律 `BlobCorruption`,**在拼接路径/触碰文件系统之前**
    就拒绝。这是 traversal-oracle 防线:没有这道校验,`"../../x"`/绝对路径
    这类输入会被 `Path.__truediv__` 原样拼接后拿去 `read_bytes()`/`exists()`,
    虽然当前调用方(scorer 等)只会传入自己 `put()` 算出的合法 sha、这条路径
    理论上不可达,但便宜的输入校验不该省——错误的调用方或未来的新调用方都
    不该有机会把非法字符串当 sha 传进来。"""
    if not _SHA_RE.fullmatch(sha):
        raise BlobCorruption(f"非法 sha 格式(应为 64 位小写十六进制): {sha!r}")


class BlobStore:
    """内容寻址 blob 存储:`root/blobs/<sha256>`。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.blobs_dir = self.root / "blobs"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.blobs_dir, _DIR_MODE)

    def _fsync_dir(self) -> None:
        fd = os.open(self.blobs_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def put(self, obj: dict | str) -> str:
        data = canonical_bytes(obj)
        sha = sha256_hex(data)
        target = self.blobs_dir / sha

        tmp_name = f".tmp.{os.getpid()}.{uuid.uuid4().hex}"
        tmp_path = self.blobs_dir / tmp_name

        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, _FILE_MODE)  # umask 可能已冲掉 os.open 的 mode,显式再钉一次
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        if target.exists():
            existing = target.read_bytes()
            existing_sha = sha256_hex(existing)
            tmp_path.unlink(missing_ok=True)  # 幂等去重:内容已在盘上,tmp 是多余的
            if existing_sha != sha:
                raise BlobCorruption(
                    f"blob {sha} 已存在但磁盘内容哈希不符(实际 {existing_sha})——"
                    "拒绝覆盖,疑似损坏或碰撞。"
                )
            return sha

        os.rename(tmp_path, target)
        self._fsync_dir()
        return sha

    def get(self, sha: str) -> str:
        _assert_valid_sha(sha)
        target = self.blobs_dir / sha
        data = target.read_bytes()
        actual_sha = sha256_hex(data)
        if actual_sha != sha:
            raise BlobCorruption(
                f"blob {sha} 读回校验失败(实际内容哈希 {actual_sha})——疑似篡改或损坏。"
            )
        return data.decode("utf-8")

    def exists(self, sha: str) -> bool:
        _assert_valid_sha(sha)
        return (self.blobs_dir / sha).exists()


@dataclass(frozen=True)
class Snapshot:
    payload_clamped: dict
    truncated: bool
    payload_sha: str
    passage_text: str
    passage_sha: str


def build_snapshots(candidate_payload: dict, mem_type: str, cap: int, tokenizer) -> Snapshot:
    """双快照组装:payload 侧走 `contract.clamp_payload`(agent 实际所见,cap
    用其默认值 8000);passage 侧走 `probe_passage.build_passage`(打分锚,
    token cap = 本函数入参 `cap`,与探针冻结规格同源、禁止复制)。"""
    payload_clamped, truncated = contract.clamp_payload(candidate_payload, mem_type)
    payload_sha = sha256_hex(canonical_bytes(payload_clamped))

    passage_text = probe_passage.build_passage(
        candidate_payload, mem_type, spec="prod", cap=cap, tokenizer=tokenizer
    )
    passage_sha = sha256_hex(canonical_bytes(passage_text))

    return Snapshot(
        payload_clamped=payload_clamped,
        truncated=truncated,
        payload_sha=payload_sha,
        passage_text=passage_text,
        passage_sha=passage_sha,
    )
