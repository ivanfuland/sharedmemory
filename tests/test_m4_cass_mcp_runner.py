# tests/test_m4_cass_mcp_runner.py
import stat, textwrap, time
from pathlib import Path
from cass_mcp import runner

def _fake_cass(tmp_path, body):
    p = tmp_path / "fake-cass"; p.write_text(f"#!/usr/bin/env bash\n{body}\n"); p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)

def test_run_ok_parses_json(tmp_path):
    cass = _fake_cass(tmp_path, 'echo \'{"results":[{"agent":"claude_code"}]}\'')
    r = runner.run_cass("search", ["q"], cass_bin=cass)
    assert r["results"][0]["agent"] == "claude_code"

def test_run_nonzero_exit_maps_error(tmp_path):
    cass = _fake_cass(tmp_path, 'echo "bad" >&2; exit 3')
    r = runner.run_cass("search", ["q"], cass_bin=cass)
    assert r["error"] == "cass_exit" and r["code"] == 3

def test_run_timeout(tmp_path):
    cass = _fake_cass(tmp_path, 'sleep 3')
    r = runner.run_cass("search", ["q"], cass_bin=cass, timeout_s=0.3)
    assert r["error"] == "timeout"

def test_run_truncates_json_returns_error(tmp_path):
    cass = _fake_cass(tmp_path, 'python3 -c "print(\'x\'*100000)"')
    r = runner.run_cass("search", ["q"], cass_bin=cass, max_bytes=1000)   # want_json=True
    assert r["error"] == "result_too_large" and r["bytes"] > 1000

def test_run_truncates_text_returns_partial(tmp_path):
    cass = _fake_cass(tmp_path, 'python3 -c "print(\'y\'*100000)"')
    r = runner.run_cass("export", ["/p"], cass_bin=cass, want_json=False, max_bytes=1000)
    assert r.get("truncated") is True and "text" in r

def test_run_export_returns_text_not_json(tmp_path):
    cass = _fake_cass(tmp_path, 'printf "# Session\\nhello markdown\\n"')   # 非 JSON 输出
    r = runner.run_cass("export", ["/p/s.jsonl", "--format", "markdown"], cass_bin=cass, want_json=False)   # <PATH> 位置参数
    assert "markdown" in r["text"] and "error" not in r

def test_circuit_breaker_opens_after_5_failures(tmp_path):
    cass = _fake_cass(tmp_path, 'exit 9')
    cb = runner.CircuitBreaker(threshold=5, cooldown_s=300)
    for _ in range(5): runner.run_cass("search", ["q"], cass_bin=cass, breaker=cb)
    r = runner.run_cass("search", ["q"], cass_bin=cass, breaker=cb)
    assert r["error"] == "unavailable"


def test_breaker_counts_bad_json(tmp_path):
    """P1-2: bad_json（exit 0 但输出非 JSON）也计入熔断失败计数。"""
    cass = _fake_cass(tmp_path, 'echo "not json"')      # exit 0 但非 JSON
    cb = runner.CircuitBreaker(threshold=5, cooldown_s=300)
    for _ in range(5): runner.run_cass("search", ["q"], cass_bin=cass, breaker=cb)
    r = runner.run_cass("search", ["q"], cass_bin=cass, breaker=cb)
    assert r["error"] == "unavailable"


def test_breaker_counts_result_too_large(tmp_path):
    """P1-2: result_too_large（exit 0 但超 max_bytes）也计入熔断失败计数。"""
    cass = _fake_cass(tmp_path, 'python3 -c "print(\'x\'*100000)"')
    cb = runner.CircuitBreaker(threshold=5, cooldown_s=300)
    for _ in range(5): runner.run_cass("search", ["q"], cass_bin=cass, max_bytes=1000, breaker=cb)
    r = runner.run_cass("search", ["q"], cass_bin=cass, max_bytes=1000, breaker=cb)
    assert r["error"] == "unavailable"


def test_default_cap_passes_200kb_json(tmp_path):
    """默认 256KB cap 放行 ~200KB JSON（timeline 7d 实测 204KB），不误判 result_too_large。"""
    cass = _fake_cass(tmp_path, "python3 -c 'import json,sys; sys.stdout.write(json.dumps({\"d\":\"x\"*200000}))'")
    r = runner.run_cass("timeline", ["--since", "7d"], cass_bin=cass)
    assert "error" not in r and len(r.get("d", "")) == 200000


def test_default_cap_rejects_over_256kb_json(tmp_path):
    """超 256KB 仍回清晰 result_too_large（病态宽窗口的 backstop）。"""
    cass = _fake_cass(tmp_path, "python3 -c 'import json,sys; sys.stdout.write(json.dumps({\"d\":\"x\"*300000}))'")
    r = runner.run_cass("timeline", ["--since", "30d"], cass_bin=cass)
    assert r["error"] == "result_too_large" and r["bytes"] > 262144


def test_oversize_is_failure_false_does_not_count_toward_breaker(tmp_path):
    """codex P1 根治点：oversize_is_failure=False 时，too-large 不计入熔断失败计数
    （search 的合理 over-fetch 超 raw cap 不该拖垮其他工具的共享熔断）。"""
    cass = _fake_cass(tmp_path, 'python3 -c "print(\'x\'*100000)"')
    cb = runner.CircuitBreaker(threshold=5, cooldown_s=300)
    for _ in range(5):
        r = runner.run_cass("search", ["q"], cass_bin=cass, max_bytes=1000, breaker=cb, oversize_is_failure=False)
        assert r["error"] == "result_too_large"
    assert cb.fails == 0
    r = runner.run_cass("search", ["q"], cass_bin=cass, max_bytes=1000, breaker=cb, oversize_is_failure=False)
    assert r["error"] == "result_too_large"   # 仍能正常报错，只是不算熔断失败


def test_oversize_is_failure_default_true_still_counts_toward_breaker(tmp_path):
    """对照：不传 oversize_is_failure（默认 True）时行为不变——too-large 仍计入熔断，
    5 次后开启（回归 test_breaker_counts_result_too_large 的行为不被本次改动破坏）。"""
    cass = _fake_cass(tmp_path, 'python3 -c "print(\'x\'*100000)"')
    cb = runner.CircuitBreaker(threshold=5, cooldown_s=300)
    for _ in range(5):
        runner.run_cass("search", ["q"], cass_bin=cass, max_bytes=1000, breaker=cb)
    assert cb.fails == 5
    r = runner.run_cass("search", ["q"], cass_bin=cass, max_bytes=1000, breaker=cb)
    assert r["error"] == "unavailable"   # 熔断已开
