"""Task 3: SessionStart digest builder 测试。
6 builder 单元测试 + 1 cc adapter 端到端 fail-soft subprocess 测试。"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "hooks"))
import gbrain_digest as gd


def test_parse_query_lines():
    raw = "[0.81] projects/x -- # X\nbody\n[0.55] people/y -- # Y\n"
    hits = gd.parse_query(raw)
    assert hits[0] == (0.81, "projects/x", "# X"), hits[0]
    assert hits[1][0] == 0.55 and hits[1][1] == "people/y"


def test_threshold_filters_low_scores():
    raw = "[0.81] projects/x -- # X\n[0.40] people/y -- # Y\n"
    d = gd.build_digest_from_raw(raw, threshold=0.6, max_tokens=1500)
    assert d["injected"] and d["hits"] == 1 and "projects/x" in d["context"]
    assert "people/y" not in d["context"]


def test_below_threshold_injects_empty():
    raw = "[0.40] people/y -- # Y\n"
    d = gd.build_digest_from_raw(raw, threshold=0.6)
    assert d["injected"] is False and d["context"] == "" and "无相关" in d["status"]


def test_stale_page_degraded():
    raw = "[0.81] decisions/z (stale) -- # 旧结论\n"
    d = gd.build_digest_from_raw(raw, threshold=0.6)
    assert d["injected"] and "stale" in d["context"].lower() and "待整编" in d["context"]


def test_truncate_to_max_tokens():
    raw = "".join(
        f"[0.9{i % 10}] projects/p{i} -- # 页{i} " + "字" * 200 + "\n"
        for i in range(50)
    )
    d = gd.build_digest_from_raw(raw, threshold=0.5, max_tokens=300)
    # 粗算 token≈len/4（中文），硬上限不破
    assert len(d["context"]) <= 300 * 4, f"截断失效 len={len(d['context'])}"
    assert d["injected"]


def test_fail_soft_on_query_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("gbrain down")

    monkeypatch.setattr(gd, "_run_query", boom)
    d = gd.build_digest("任意", threshold=0.6)
    assert d["injected"] is False and d["context"] == "" and "不可用" in d["status"]


def test_cc_adapter_failsoft_always_valid_json():
    """★ 端到端（非 monkeypatch）：真跑 cc_sessionstart.sh 三种输入——正常/query不可达/畸形env——
    每种必须 exit 0 + 合法 JSON + additionalContext 非空（注空含 [记忆层]）。codex R1 #8/#12。"""
    adapter = str(
        pathlib.Path(__file__).resolve().parent.parent / "hooks" / "cc_sessionstart.sh"
    )
    cases = [
        {"CLAUDE_PROJECT_DIR": os.path.expanduser("~/projects/sharedmemory")},  # 正常
        {"GBRAIN_HOME": "/nonexistent", "CLAUDE_PROJECT_DIR": "/tmp"},  # query 不可达
        {"CLAUDE_PROJECT_DIR": "/x; rm -rf $(echo)"},  # 畸形 env
    ]
    for env_extra in cases:
        r = subprocess.run(
            ["bash", adapter],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **env_extra},
        )
        assert r.returncode == 0, (
            f"adapter 必须 exit 0（fail-soft）: env={env_extra} "
            f"rc={r.returncode} {r.stderr[:200]}"
        )
        out = json.loads(r.stdout)  # 合法 JSON 否则抛
        ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert ctx, f"additionalContext 不得为空（注空也要状态行）: {env_extra}"
