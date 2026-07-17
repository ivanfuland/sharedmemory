"""blobstore.py 的测试(P4 §Task 3:内容寻址 + 双快照)。

固定纪律:
- `put()` 幂等去重:同内容 → 同 sha256,磁盘只留一份文件。
- 写协议:唯一 tmp 名(pid+uuid)写入+fsync → 目标已存在则读回校验(不符
  raise `BlobCorruption`),否则 rename+目录 fsync。
- `get()` 读时重算 hash,不符 raise `BlobCorruption`(篡改检测)。
- passage 快照必须调用 `everos_eval.probe_passage.build_passage`,禁止
  在本模块内复制打分口径逻辑——用同一个 tokenizer 对同一 payload 分别调
  两条路径,断言逐字节相同。真 tokenizer 用例标 `slow`(需要本机 pinned
  HF tokenizer 缓存);另设 fake tokenizer 单测跑常规 CI,不依赖网络/缓存。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from everos_mcp import blobstore, contract
from everos_eval import probe_passage


# ======================================================================
# fake tokenizer(char-level,确定性,零网络依赖)
# ======================================================================

class _FakeTokenizer:
    """字符级 fake tokenizer:每个字符 = 一个 token id(ord)。仅用于验证
    build_snapshots 与 probe_passage.build_passage 走同一条代码路径,不用于
    验证真实截断口径(真口径由 slow 用例覆盖真 tokenizer)。"""

    def encode(self, text, add_special_tokens=False):
        return [ord(c) for c in text]

    def decode(self, ids, skip_special_tokens=True):
        return "".join(chr(i) for i in ids)


CASE_PAYLOAD = {
    "task_intent": "对抗性审查 EverOS spec",
    "approach": "先读 spec 再核代码公开 API 面",
    "key_insight": "spec 假设必须映射到公开 API 面才算验证",
}


# ======================================================================
# BlobStore.put / get / exists
# ======================================================================

def test_put_idempotent_dedup_same_content_single_file(tmp_path):
    store = blobstore.BlobStore(tmp_path / "ledger")
    obj = {"b": 1, "a": 2}
    sha1 = store.put(obj)
    sha2 = store.put(dict(obj))  # 不同 dict 实例,同内容
    assert sha1 == sha2
    files = list((tmp_path / "ledger" / "blobs").glob("*"))
    assert files == [tmp_path / "ledger" / "blobs" / sha1]


def test_put_dict_canonical_serialization_sorted_keys(tmp_path):
    store = blobstore.BlobStore(tmp_path / "ledger")
    obj = {"z": 1, "a": 2}
    sha = store.put(obj)
    got = store.get(sha)
    assert got == json.dumps(obj, ensure_ascii=False, sort_keys=True)
    assert json.loads(got) == obj


def test_put_str_stored_as_is(tmp_path):
    store = blobstore.BlobStore(tmp_path / "ledger")
    text = "纯文本快照内容"
    sha = store.put(text)
    assert store.get(sha) == text


def test_exists(tmp_path):
    store = blobstore.BlobStore(tmp_path / "ledger")
    sha = store.put("hello")
    assert store.exists(sha)
    assert not store.exists("0" * 64)


def test_get_after_tampering_raises_blobcorruption(tmp_path):
    store = blobstore.BlobStore(tmp_path / "ledger")
    sha = store.put("original content")
    blob_path = tmp_path / "ledger" / "blobs" / sha
    blob_path.write_bytes(b"tampered bytes")
    with pytest.raises(blobstore.BlobCorruption):
        store.get(sha)


def test_target_exists_but_content_differs_raises_on_put(tmp_path):
    store = blobstore.BlobStore(tmp_path / "ledger")
    # 先正常写入,拿到真实 sha
    sha = store.put("real content")
    # 直接在磁盘上把这份 blob 篡改成跟文件名(sha)不匹配的内容,模拟"目标已
    # 存在但内容跟哈希对不上"(损坏/攻击场景),不依赖真实 sha256 碰撞。
    blob_path = tmp_path / "ledger" / "blobs" / sha
    blob_path.write_bytes(b"corrupted on disk")
    with pytest.raises(blobstore.BlobCorruption):
        store.put("real content")  # 同样内容,算出同一个 sha,读回校验发现对不上


@pytest.mark.parametrize("bad_sha", ["../../x", "/etc/hostname", "zz", "", "A" * 64, "0" * 63])
def test_get_rejects_malformed_or_traversal_shaped_sha(tmp_path, bad_sha):
    """M3.1:`get()` 必须在拼路径/碰文件系统之前拒绝任何不是 64 位小写十六进制
    的输入——traversal-shaped(`../../x`/绝对路径)、长度不对、大写字母都算。"""
    store = blobstore.BlobStore(tmp_path / "ledger")
    with pytest.raises(blobstore.BlobCorruption):
        store.get(bad_sha)


@pytest.mark.parametrize("bad_sha", ["../../x", "/etc/hostname", "zz"])
def test_exists_rejects_malformed_or_traversal_shaped_sha_without_touching_fs(tmp_path, bad_sha):
    """`exists()` 与 `get()` 同一 fail-closed 语义(拒绝而非静默返回 False)。"""
    store = blobstore.BlobStore(tmp_path / "ledger")
    with pytest.raises(blobstore.BlobCorruption):
        store.exists(bad_sha)


def test_get_and_exists_accept_well_formed_sha_unchanged(tmp_path):
    """回归防护:合法 sha(自己 put() 算出的那种)不受新增校验影响。"""
    store = blobstore.BlobStore(tmp_path / "ledger")
    sha = store.put("normal content")
    assert store.exists(sha) is True
    assert store.get(sha) == "normal content"


def test_stray_tmp_files_harmless(tmp_path):
    store = blobstore.BlobStore(tmp_path / "ledger")
    blobs_dir = tmp_path / "ledger" / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    stray = blobs_dir / ".tmp.99999.deadbeefdeadbeef"
    stray.write_bytes(b"orphaned tmp junk")

    sha = store.put({"k": "v"})
    assert store.get(sha) == json.dumps({"k": "v"}, ensure_ascii=False, sort_keys=True)
    assert store.exists(sha)
    # stray tmp 文件依然在盘上未被清理,但完全不影响后续正常 put/get 走通
    assert stray.exists()
    assert stray.read_bytes() == b"orphaned tmp junk"


def test_permissions_blobs_dir_0700_and_files_0600(tmp_path):
    store = blobstore.BlobStore(tmp_path / "ledger")
    sha = store.put("perm check")
    blobs_dir = tmp_path / "ledger" / "blobs"
    blob_file = blobs_dir / sha

    dir_mode = stat.S_IMODE(blobs_dir.stat().st_mode)
    file_mode = stat.S_IMODE(blob_file.stat().st_mode)
    assert dir_mode == 0o700
    assert file_mode == 0o600


def test_no_tmp_files_left_after_successful_put(tmp_path):
    store = blobstore.BlobStore(tmp_path / "ledger")
    store.put("clean up after me")
    blobs_dir = tmp_path / "ledger" / "blobs"
    leftover_tmp = [p for p in blobs_dir.glob(".tmp.*")]
    assert leftover_tmp == []


# ======================================================================
# build_snapshots:双快照(payload 走 contract.clamp_payload,passage 走
# probe_passage.build_passage,禁止在本模块复制打分口径)
# ======================================================================

def test_build_snapshots_payload_matches_clamp_payload(tmp_path):
    tok = _FakeTokenizer()
    snap = blobstore.build_snapshots(CASE_PAYLOAD, "agent_case", cap=2048, tokenizer=tok)
    expected_payload, expected_truncated = contract.clamp_payload(CASE_PAYLOAD, "agent_case")
    assert snap.payload_clamped == expected_payload
    assert snap.truncated == expected_truncated


def test_build_snapshots_passage_byte_identical_to_probe_passage_fake_tokenizer(tmp_path):
    tok = _FakeTokenizer()
    snap = blobstore.build_snapshots(CASE_PAYLOAD, "agent_case", cap=2048, tokenizer=tok)
    direct = probe_passage.build_passage(CASE_PAYLOAD, "agent_case", spec="prod", cap=2048, tokenizer=tok)
    assert snap.passage_text == direct


def test_build_snapshots_passage_truncation_matches_probe_passage_fake_tokenizer(tmp_path):
    tok = _FakeTokenizer()
    long_payload = dict(CASE_PAYLOAD, approach="很长的过程描述。" * 500)
    snap = blobstore.build_snapshots(long_payload, "agent_case", cap=32, tokenizer=tok)
    direct = probe_passage.build_passage(long_payload, "agent_case", spec="prod", cap=32, tokenizer=tok)
    assert snap.passage_text == direct
    assert len(tok.encode(snap.passage_text)) <= 32 + 2


def test_build_snapshots_shas_are_content_addressed_and_consistent_with_blobstore(tmp_path):
    store = blobstore.BlobStore(tmp_path / "ledger")
    tok = _FakeTokenizer()
    snap = blobstore.build_snapshots(CASE_PAYLOAD, "agent_case", cap=2048, tokenizer=tok)

    payload_sha = store.put(snap.payload_clamped)
    passage_sha = store.put(snap.passage_text)

    assert snap.payload_sha == payload_sha
    assert snap.passage_sha == passage_sha
    assert store.get(payload_sha) == json.dumps(snap.payload_clamped, ensure_ascii=False, sort_keys=True)
    assert store.get(passage_sha) == snap.passage_text


def test_build_snapshots_missing_field_raises_keyerror_from_probe_passage(tmp_path):
    tok = _FakeTokenizer()
    bad = {k: v for k, v in CASE_PAYLOAD.items() if k != "task_intent"}
    with pytest.raises(KeyError):
        blobstore.build_snapshots(bad, "agent_case", cap=2048, tokenizer=tok)


@pytest.mark.slow
def test_build_snapshots_passage_byte_identical_to_probe_passage_real_tokenizer():
    tok = probe_passage.rerank_tokenizer()
    snap = blobstore.build_snapshots(CASE_PAYLOAD, "agent_case", cap=2048, tokenizer=tok)
    direct = probe_passage.build_passage(CASE_PAYLOAD, "agent_case", spec="prod", cap=2048, tokenizer=tok)
    assert snap.passage_text == direct
