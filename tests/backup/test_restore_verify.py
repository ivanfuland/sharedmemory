"""restore-cass.sh step 8 的 doctor 验证门（infra/backup/cass/restore_verify.py）单测。

不跑真实 restore——直接喂合成 `cass doctor --json` 给 validator，验它对四类形态的判定：
通过 / summary 非零 / verified_blob_count<=0（零错误 vs 没检查同形）/ JSON 坏。
"""
from __future__ import annotations

import json

import pytest

from restore_verify import check

_GOOD_SUMMARY = {
    "missing_blob_count": 0,
    "checksum_mismatch_count": 0,
    "manifest_checksum_mismatch_count": 0,
    "invalid_manifest_count": 0,
    "interrupted_capture_count": 0,
    "verified_blob_count": 3284,
    "manifest_count": 4021,
    "duplicate_blob_reference_count": 737,
}


def _doctor(summary: dict, status: str = "verified") -> str:
    return json.dumps({"raw_mirror": {"status": status, "summary": summary}})


def test_all_zero_and_verified_blob_count_positive_passes():
    msg = check(_doctor(_GOOD_SUMMARY))
    assert "OK" in msg
    assert "verified_blob_count=3284" in msg


def test_nonzero_error_counter_fails():
    s = dict(_GOOD_SUMMARY, missing_blob_count=2)
    with pytest.raises(SystemExit) as ei:
        check(_doctor(s))
    assert "missing_blob_count" in str(ei.value)


def test_verified_blob_count_zero_fails_even_with_all_errors_zero():
    # 核心：所有错误计数器都 0，但 verified_blob_count=0 —— 「根本没检查」的伪装，必须 FAIL
    s = dict(_GOOD_SUMMARY, verified_blob_count=0)
    with pytest.raises(SystemExit) as ei:
        check(_doctor(s))
    assert "verified_blob_count" in str(ei.value)


def test_missing_required_counter_fails():
    s = dict(_GOOD_SUMMARY)
    del s["checksum_mismatch_count"]
    with pytest.raises(SystemExit) as ei:
        check(_doctor(s))
    assert "checksum_mismatch_count" in str(ei.value)


def test_missing_summary_fails():
    with pytest.raises(SystemExit) as ei:
        check(json.dumps({"raw_mirror": {"status": "verified"}}))
    assert "raw_mirror.summary" in str(ei.value)


def test_malformed_json_fails():
    with pytest.raises(SystemExit) as ei:
        check("this is not json")
    assert "解析失败" in str(ei.value)


def test_non_int_counter_value_fails():
    s = dict(_GOOD_SUMMARY, verified_blob_count="lots")
    with pytest.raises(SystemExit) as ei:
        check(_doctor(s))
    assert "verified_blob_count" in str(ei.value)


def test_numeric_string_counter_fails_fail_closed():
    # 数字字符串 "3284" 也拒（严格 int-only，对齐 Tier0 gate 口径）——doctor 计数器本该是 int
    s = dict(_GOOD_SUMMARY, verified_blob_count="3284")
    with pytest.raises(SystemExit) as ei:
        check(_doctor(s))
    assert "verified_blob_count" in str(ei.value)


def test_status_warn_fails_even_with_summary_all_zero():
    # summary 全 0 + verified_blob_count>0，但 raw_mirror.status=warn → 自相矛盾，fail-closed 拒
    with pytest.raises(SystemExit) as ei:
        check(_doctor(_GOOD_SUMMARY, status="warn"))
    assert "status" in str(ei.value)
