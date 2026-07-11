"""infra/backup/cass/cass_common.py 的单元测试。

覆盖 Task 2 brief 的全部测试要点：
  - canonical JSON 字节稳定（同 dict 两次 dumps 字节相等；键序无关；ensure_ascii=False）
  - sha256_file / blake3_file 流式结果与标准库/blake3 直算一致
  - blake3_file(prefix_len=k) 与全量前 k 字节单独算一致
  - fadvise=True/False 时 os.posix_fadvise 被调用次数（monkeypatch 记录调用）
  - read_digest：不存在返回 None；解析失败 raise；存在时按字节读取后 json.loads
  - latest_published：按 digest.json 的 generation 选最大者，而非 mtime
    （造两个假备份目录，os.utime 把旧的摸新，仍必须选 generation 大者）
  - state_write_atomic → state_read roundtrip
  - 篡改任意一行 / 删首行 → StateCorrupt
"""
from __future__ import annotations

import hashlib
import json
import os

import blake3 as blake3_module
import pytest

import cass_common
from cass_common import SessionRec, StateCorrupt


# ---------------------------------------------------------------------------
# dumps_canonical
# ---------------------------------------------------------------------------


def test_dumps_canonical_stable_across_key_order():
    a = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
    out_a = cass_common.dumps_canonical(a)
    out_b = cass_common.dumps_canonical(b)
    assert out_a == out_b


def test_dumps_canonical_two_calls_same_dict_identical_bytes():
    obj = {"generation": 3, "backup_name": "cass-20260710-000000-1", "tables": {"messages": {"max_id": 10}}}
    assert cass_common.dumps_canonical(obj) == cass_common.dumps_canonical(obj)


def test_dumps_canonical_matches_expected_format():
    obj = {"b": 1, "a": "中文"}
    out = cass_common.dumps_canonical(obj)
    assert out == b'{"a":"\xe4\xb8\xad\xe6\x96\x87","b":1}'
    assert isinstance(out, bytes)


# ---------------------------------------------------------------------------
# sha256_file / blake3_file
# ---------------------------------------------------------------------------


def test_sha256_file_matches_hashlib(tmp_path):
    p = tmp_path / "blob.bin"
    payload = os.urandom(3 * 1024 * 1024 + 17)  # >1 MiB, forces multi-chunk streaming
    p.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert cass_common.sha256_file(p) == expected


def test_blake3_file_matches_blake3_direct(tmp_path):
    p = tmp_path / "blob.bin"
    payload = os.urandom(2 * 1024 * 1024 + 5)
    p.write_bytes(payload)
    expected = blake3_module.blake3(payload).hexdigest()
    assert cass_common.blake3_file(p) == expected


def test_blake3_file_prefix_len_matches_manual_prefix_hash(tmp_path):
    p = tmp_path / "blob.bin"
    payload = os.urandom(2 * 1024 * 1024 + 123)
    p.write_bytes(payload)
    k = 1024 * 1024 + 42  # spans past one full 1 MiB chunk boundary
    expected = blake3_module.blake3(payload[:k]).hexdigest()
    assert cass_common.blake3_file(p, prefix_len=k) == expected


def test_blake3_file_prefix_len_larger_than_file_hashes_whole_file(tmp_path):
    p = tmp_path / "blob.bin"
    payload = os.urandom(100)
    p.write_bytes(payload)
    expected = blake3_module.blake3(payload).hexdigest()
    assert cass_common.blake3_file(p, prefix_len=10_000) == expected


# ---------------------------------------------------------------------------
# fadvise
# ---------------------------------------------------------------------------


def test_sha256_file_fadvise_true_calls_posix_fadvise_once(tmp_path, monkeypatch):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"some content")
    calls = []
    real_fadvise = os.posix_fadvise

    def _spy(fd, offset, length, advice):
        calls.append((offset, length, advice))
        return real_fadvise(fd, offset, length, advice)

    monkeypatch.setattr(os, "posix_fadvise", _spy)
    cass_common.sha256_file(p, fadvise=True)
    assert len(calls) == 1
    assert calls[0] == (0, 0, os.POSIX_FADV_DONTNEED)


def test_sha256_file_fadvise_false_never_calls_posix_fadvise(tmp_path, monkeypatch):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"some content")
    calls = []
    monkeypatch.setattr(os, "posix_fadvise", lambda *a, **kw: calls.append(a))
    cass_common.sha256_file(p, fadvise=False)
    assert calls == []


def test_blake3_file_fadvise_true_calls_posix_fadvise_once(tmp_path, monkeypatch):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"some content")
    calls = []
    real_fadvise = os.posix_fadvise

    def _spy(fd, offset, length, advice):
        calls.append((offset, length, advice))
        return real_fadvise(fd, offset, length, advice)

    monkeypatch.setattr(os, "posix_fadvise", _spy)
    cass_common.blake3_file(p, fadvise=True, prefix_len=4)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# read_digest
# ---------------------------------------------------------------------------


def test_read_digest_missing_returns_none(tmp_path):
    assert cass_common.read_digest(tmp_path / "nonexistent-dir") is None


def test_read_digest_reads_bytes_then_json_loads(tmp_path):
    backup_dir = tmp_path / "cass-20260710-000000-1"
    backup_dir.mkdir()
    digest = {"generation": 1, "backup_name": backup_dir.name}
    (backup_dir / "digest.json").write_bytes(cass_common.dumps_canonical(digest))
    assert cass_common.read_digest(backup_dir) == digest


def test_read_digest_parse_failure_raises(tmp_path):
    backup_dir = tmp_path / "cass-broken"
    backup_dir.mkdir()
    (backup_dir / "digest.json").write_bytes(b"{not json")
    with pytest.raises(json.JSONDecodeError):
        cass_common.read_digest(backup_dir)


# ---------------------------------------------------------------------------
# latest_published
# ---------------------------------------------------------------------------


def _make_published_backup(dest, name, generation, mtime=None):
    backup_dir = dest / name
    backup_dir.mkdir(parents=True)
    digest = {"generation": generation, "backup_name": name}
    (backup_dir / "digest.json").write_bytes(cass_common.dumps_canonical(digest))
    (backup_dir / "COMPLETE").write_bytes(b"")
    if mtime is not None:
        os.utime(backup_dir, (mtime, mtime))
        os.utime(backup_dir / "digest.json", (mtime, mtime))
        os.utime(backup_dir / "COMPLETE", (mtime, mtime))
    return backup_dir


def test_latest_published_none_when_dest_empty(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    assert cass_common.latest_published(dest) is None


def test_latest_published_picks_max_generation_not_mtime(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    # 旧的（generation 更大）先建，新的（generation 更小）后建，
    # 再把旧的 mtime 摸得比新的更"新"——仍必须按 generation 选。
    old_but_higher_generation = _make_published_backup(dest, "cass-20260710-000000-5", generation=5)
    _make_published_backup(dest, "cass-20260711-000000-3", generation=3)
    newer_mtime = 2_000_000_000  # 远未来
    os.utime(old_but_higher_generation, (newer_mtime, newer_mtime))
    os.utime(old_but_higher_generation / "digest.json", (newer_mtime, newer_mtime))
    os.utime(old_but_higher_generation / "COMPLETE", (newer_mtime, newer_mtime))

    result = cass_common.latest_published(dest)
    assert result is not None
    name, digest = result
    assert name == "cass-20260710-000000-5"
    assert digest["generation"] == 5


def test_latest_published_skips_dirs_without_complete_marker(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_published_backup(dest, "cass-20260710-000000-1", generation=1)
    # 无 COMPLETE 的高 generation 目录必须被跳过
    no_complete = dest / "cass-20260711-000000-9"
    no_complete.mkdir()
    (no_complete / "digest.json").write_bytes(cass_common.dumps_canonical({"generation": 9}))

    name, digest = cass_common.latest_published(dest)
    assert name == "cass-20260710-000000-1"
    assert digest["generation"] == 1


def test_latest_published_skips_dirs_without_generation_field(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_published_backup(dest, "cass-20260710-000000-1", generation=1)
    weird = dest / "cass-20260712-000000-x"
    weird.mkdir()
    (weird / "digest.json").write_bytes(cass_common.dumps_canonical({"backup_name": "cass-20260712-000000-x"}))
    (weird / "COMPLETE").write_bytes(b"")

    name, digest = cass_common.latest_published(dest)
    assert name == "cass-20260710-000000-1"


def test_latest_published_all_missing_generation_returns_none(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    weird = dest / "cass-20260712-000000-x"
    weird.mkdir()
    (weird / "digest.json").write_bytes(cass_common.dumps_canonical({"backup_name": "x"}))
    (weird / "COMPLETE").write_bytes(b"")
    assert cass_common.latest_published(dest) is None


# ---------------------------------------------------------------------------
# codex R4-P0：strict 基线选择——含 COMPLETE 但 generation 不可读/非法的成员，
# 基线选择绝不静默 skip 退回更老一份（那会选错 tip、把缩水的坏备份当好备份）。
# ---------------------------------------------------------------------------


def test_latest_published_strict_raises_on_unreadable_generation(tmp_path):
    """codex 复现的核心机理：gen1 有效 + 一个更新的目录含 COMPLETE 但 generation
    坏（字符串）。非 strict（轮转语义）宽容跳过、退回 gen1；strict（基线选择）
    必须 raise，绝不静默退回更老基线。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_published_backup(dest, "cass-20260710-000000-1", generation=1)
    newer = dest / "cass-20260711-000000-2"
    newer.mkdir()
    # generation 是字符串（非法）——真实 tip 不可读。
    (newer / "digest.json").write_bytes(cass_common.dumps_canonical({"generation": "2"}))
    (newer / "COMPLETE").write_bytes(b"")

    # 非 strict：宽容 skip，静默退回 gen1（这正是危险的旧行为）。
    lenient = cass_common.latest_published(dest)
    assert lenient is not None and lenient[0] == "cass-20260710-000000-1"

    # strict：必须 raise，指认坏成员。
    with pytest.raises(cass_common.PublishedScanError) as excinfo:
        cass_common.latest_published(dest, strict=True)
    assert "cass-20260711-000000-2" in str(excinfo.value)


def test_latest_published_strict_raises_on_missing_digest_in_complete_dir(tmp_path):
    """含 COMPLETE 但完全没有 digest.json 的目录，strict 同样 raise（COMPLETE 是
    最后写的，缺 digest.json 是完整性事件）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_published_backup(dest, "cass-20260710-000000-1", generation=1)
    bare = dest / "cass-20260711-000000-9"
    bare.mkdir()
    (bare / "COMPLETE").write_bytes(b"")  # 无 digest.json

    with pytest.raises(cass_common.PublishedScanError):
        cass_common.latest_published(dest, strict=True)


def test_latest_published_strict_clean_set_matches_lenient(tmp_path):
    """对照：基线集全部干净时，strict 与非 strict 返回同一个 tip（strict 只在有
    坏成员时才 raise，不改变正常路径）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_published_backup(dest, "cass-a", generation=1)
    _make_published_backup(dest, "cass-b", generation=2)

    assert cass_common.latest_published(dest, strict=True) == cass_common.latest_published(dest)
    assert cass_common.latest_published(dest, strict=True)[0] == "cass-b"


def test_latest_published_strict_empty_returns_none(tmp_path):
    """无任何含 COMPLETE 的 cass-*/：strict 也返回 None（首晚，不是错误）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    assert cass_common.latest_published(dest, strict=True) is None


def test_rotation_victims_still_lenient_skips_bad_generation(tmp_path):
    """同族自查：轮转选点仍走宽容 skip（少删安全）——含 COMPLETE 但 generation 坏
    的目录不参与轮转、也不出现在待删名单里（不因 R4-P0 的 strict 改动被牵连）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    for i in range(1, 4):
        _make_published_backup(dest, f"cass-{i}", generation=i)
    bad = dest / "cass-badgen"
    bad.mkdir()
    (bad / "digest.json").write_bytes(cass_common.dumps_canonical({"generation": "x"}))
    (bad / "COMPLETE").write_bytes(b"")

    # keep=1：3 个合法成员里删最旧 2 个；坏 generation 的那个不参与、不被删。
    victims = cass_common.rotation_victims(dest, keep=1)
    assert "cass-badgen" not in victims
    assert set(victims) == {"cass-1", "cass-2"}


# ---------------------------------------------------------------------------
# codex R5-P1：pre_reset_victims —— retention_reset 清掉所有 generation < 重置点的份
# ---------------------------------------------------------------------------


def test_pre_reset_victims_selects_all_older_than_reset_point(tmp_path):
    """重置点 generation=4：gen1/2/3 全部选为待删，重置点自身（gen4）不 < 4，绝不
    被选。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    for i in range(1, 4):
        _make_published_backup(dest, f"cass-{i}", generation=i)
    _make_published_backup(dest, "cass-reset", generation=4)

    victims = cass_common.pre_reset_victims(dest, reset_generation=4)
    assert set(victims) == {"cass-1", "cass-2", "cass-3"}
    assert "cass-reset" not in victims


def test_pre_reset_victims_lenient_skips_bad_generation(tmp_path):
    """同族：读不到 generation 的目录不参与、不被删（宽容 skip，同 rotation_victims）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_published_backup(dest, "cass-1", generation=1)
    _make_published_backup(dest, "cass-reset", generation=5)
    bad = dest / "cass-badgen"
    bad.mkdir()
    (bad / "digest.json").write_bytes(cass_common.dumps_canonical({"generation": "x"}))
    (bad / "COMPLETE").write_bytes(b"")

    victims = cass_common.pre_reset_victims(dest, reset_generation=5)
    assert victims == ["cass-1"]
    assert "cass-badgen" not in victims


def test_pre_reset_victims_empty_when_reset_is_only_backup(tmp_path):
    """重置点是唯一一份（首次即 retention_reset）→ 无更旧的份，待删列表为空。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_published_backup(dest, "cass-reset", generation=1)
    assert cass_common.pre_reset_victims(dest, reset_generation=1) == []


# ---------------------------------------------------------------------------
# state_read / state_write_atomic
# ---------------------------------------------------------------------------


_SAMPLE_RECORDS = [
    SessionRec("claude-projects/foo/a.jsonl", 1234, "b3" + "a" * 62, "present"),
    SessionRec("codex-sessions/bar/b.jsonl", 5678, "b3" + "b" * 62, "present"),
    SessionRec("openclaw-agents/baz/c.jsonl", 0, "b3" + "c" * 62, "absent_at_source"),
]


def test_state_roundtrip(tmp_path):
    path = tmp_path / "sessions.state.tsv"
    cass_common.state_write_atomic(path, _SAMPLE_RECORDS)
    result = cass_common.state_read(path)
    assert result == _SAMPLE_RECORDS


def test_state_write_atomic_uses_tmp_and_replace(tmp_path):
    path = tmp_path / "sessions.state.tsv"
    cass_common.state_write_atomic(path, _SAMPLE_RECORDS)
    assert path.exists()
    assert not (tmp_path / "sessions.state.tsv.tmp").exists()  # 已 replace 掉，不留残余


def test_state_write_atomic_first_line_is_sha256_of_rest(tmp_path):
    path = tmp_path / "sessions.state.tsv"
    cass_common.state_write_atomic(path, _SAMPLE_RECORDS)
    raw = path.read_bytes()
    header, rest = raw.split(b"\n", 1)
    assert header.startswith(b"#sha256 ")
    expected = header[len(b"#sha256 "):].decode()
    assert expected == hashlib.sha256(rest).hexdigest()


def test_state_read_empty_records_roundtrip(tmp_path):
    path = tmp_path / "sessions.state.tsv"
    cass_common.state_write_atomic(path, [])
    assert cass_common.state_read(path) == []


def test_state_read_tampered_line_raises_state_corrupt(tmp_path):
    path = tmp_path / "sessions.state.tsv"
    cass_common.state_write_atomic(path, _SAMPLE_RECORDS)
    lines = path.read_bytes().split(b"\n")
    # 篡改第二行（首条记录）的 nas_size 字段，首行 checksum 不变 → 不符
    lines[1] = lines[1].replace(b"1234", b"9999")
    path.write_bytes(b"\n".join(lines))
    with pytest.raises(StateCorrupt):
        cass_common.state_read(path)


def test_state_read_deleted_header_raises_state_corrupt(tmp_path):
    path = tmp_path / "sessions.state.tsv"
    cass_common.state_write_atomic(path, _SAMPLE_RECORDS)
    lines = path.read_bytes().split(b"\n")
    body_only = b"\n".join(lines[1:])  # 删掉首行
    path.write_bytes(body_only)
    with pytest.raises(StateCorrupt):
        cass_common.state_read(path)


def test_state_read_malformed_header_prefix_raises_state_corrupt(tmp_path):
    path = tmp_path / "sessions.state.tsv"
    cass_common.state_write_atomic(path, _SAMPLE_RECORDS)
    raw = path.read_bytes()
    _, rest = raw.split(b"\n", 1)
    path.write_bytes(b"not-a-sha256-header\n" + rest)
    with pytest.raises(StateCorrupt):
        cass_common.state_read(path)
