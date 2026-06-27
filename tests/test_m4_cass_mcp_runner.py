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
