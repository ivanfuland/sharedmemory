import json, pathlib
from cass_mcp.diversify import overfetch_limit, diversify_by_session, apply_search_postprocess

def test_overfetch_limit_clamps_and_covers():
    assert overfetch_limit(10) == (10, 30)
    assert overfetch_limit(100) == (50, 150)   # clamp 到 50，overfetch 150 ≥ 50
    assert overfetch_limit(1) == (1, 3)
    assert overfetch_limit(0) == (1, 3)         # 下限 1

def test_diversify_soft_cap_and_order():
    hits = [{"source_path": "a", "i": i} for i in range(3)] + \
           [{"source_path": "b", "i": i} for i in range(3)]
    out = diversify_by_session(hits, limit=6, max_per_session=3)
    assert len(out) == 6
    assert [h["i"] for h in out[:3]] == [0, 1, 2]        # 分数序保留

def test_diversify_backfill_exceeds_cap_by_design():
    hits = [{"source_path": "a", "i": i} for i in range(10)]
    out = diversify_by_session(hits, limit=5, max_per_session=3)
    assert len(out) == 5   # best-effort：单会话回填超软上限，保证返回够数

def test_diversify_empty():
    assert diversify_by_session([], 5) == []

def test_apply_postprocess_rewrites_count_limit_keeps_clamped():
    r = {"hits": [{"source_path": "a"}, {"source_path": "a"}, {"source_path": "a"},
                  {"source_path": "a"}, {"source_path": "b"}],
         "count": 5, "limit": 30, "total_matches": 42, "hits_clamped": True}
    out = apply_search_postprocess(r, user_limit=3)
    assert len(out["hits"]) == 3
    assert out["count"] == 3
    assert out["limit"] == 3
    assert out["total_matches"] == 42       # 不动
    assert out["hits_clamped"] is True      # 不动（CASS token-budget 语义）

def test_apply_postprocess_error_passthrough():
    assert apply_search_postprocess({"error": "not_ready"}, 10) == {"error": "not_ready"}
    assert apply_search_postprocess({"no": "hits"}, 10) == {"no": "hits"}

def test_apply_postprocess_on_real_fixture():
    p = pathlib.Path(__file__).parent / "fixtures" / "cass-cli" / "search.json"
    r = json.loads(p.read_text())
    tm, hc = r["total_matches"], r["hits_clamped"]
    out = apply_search_postprocess(r, user_limit=1)
    assert out["count"] == len(out["hits"]) == 1
    assert out["limit"] == 1
    assert out["total_matches"] == tm
    assert out["hits_clamped"] == hc

def test_cass_search_desc_mentions_limit_cap(monkeypatch):
    monkeypatch.setenv("CASS_MCP_BEARER", "0" * 64)   # server import-time fail-fast 需要
    from cass_mcp import server
    assert "50" in server.CASS_SEARCH_DESC             # silent clamp 已在 description 告知


# ---- B3 fix: cass_search over-fetch 撞 256KB raw cap 回归测试（codex P1） ----

def test_cass_search_overfetch_raw_large_final_small(monkeypatch):
    """核心回归（codex 的确切场景）：raw over-fetch payload 曾在旧 1MB cap 之上、现在 8MB 之下，
    150 条 hits 集中在少数 session（模拟真实脏数据分布），仍应正常返回、砍回 user_limit，
    且传给 run_cass 的 max_bytes 必须精确等于 _SEARCH_RAW_MAX_BYTES（==8MB），
    oversize_is_failure 必须显式传 False（raw 过大不连累熔断）。"""
    monkeypatch.setenv("CASS_MCP_BEARER", "0" * 64)   # server import-time fail-fast 需要
    from cass_mcp import server, runner
    monkeypatch.setattr(server, "_readiness", lambda: {"semantic": True, "lexical": True, "infinity": True})

    # 150 条 hits，集中在 5 个 source_path（模拟真实脏数据：少数长会话贡献大量命中），
    # 每条 content 较大，模拟真实 over-fetch raw payload（远超旧 256KB/1MB cap，仍 << 8MB）
    hits = [{"source_path": f"conv-{i % 5}", "agent": "claude_code",
             "score": 1.0 - i * 0.001, "snippet": "s" * 200, "content": "c" * 2000}
            for i in range(150)]
    raw_size = len(json.dumps({"hits": hits}, ensure_ascii=False).encode("utf-8"))
    assert 262144 < raw_size < 8 * 1024 * 1024   # 场景确实曾超旧 256KB/1MB cap，但 < 新 8MB cap

    captured = {}

    def fake_run_cass(subcmd, args, *, want_json=True, cass_bin=None, timeout_s=30,
                       max_bytes=262144, breaker=None, _now=None, oversize_is_failure=True):
        captured["max_bytes"] = max_bytes
        captured["oversize_is_failure"] = oversize_is_failure
        return {"hits": hits, "count": len(hits), "limit": 150,
                "total_matches": 500, "hits_clamped": False}

    monkeypatch.setattr(runner, "run_cass", fake_run_cass)

    result = server.cass_search("some query", limit=10)

    assert "error" not in result
    assert result["count"] == 10
    assert len(result["hits"]) == 10
    assert captured["max_bytes"] == server._SEARCH_RAW_MAX_BYTES
    assert server._SEARCH_RAW_MAX_BYTES == 8 * 1024 * 1024
    assert captured["oversize_is_failure"] is False
    from collections import Counter
    counts = Counter(h["source_path"] for h in result["hits"])
    assert max(counts.values()) <= 3   # 单会话软上限（回填分支才可能超）


def test_cass_search_clamps_max_content_length(monkeypatch):
    """max_content_length clamp：调用方传超大值必须被夹到 _MAX_CONTENT_LENGTH(4000)；
    合理值（≤4000）原样透传，不被误改。"""
    monkeypatch.setenv("CASS_MCP_BEARER", "0" * 64)
    from cass_mcp import server, runner
    monkeypatch.setattr(server, "_readiness", lambda: {"semantic": True, "lexical": True, "infinity": True})

    captured = {}

    def fake_run_cass(subcmd, args, *, want_json=True, cass_bin=None, timeout_s=30,
                       max_bytes=262144, breaker=None, _now=None, oversize_is_failure=True):
        captured["args"] = args
        return {"hits": [], "count": 0, "limit": 10, "total_matches": 0, "hits_clamped": False}

    monkeypatch.setattr(runner, "run_cass", fake_run_cass)

    server.cass_search("q", max_content_length=10000)
    args = captured["args"]
    idx = args.index("--max-content-length")
    assert args[idx + 1] == "4000"   # 夹到 clamp 上限，不是 10000

    server.cass_search("q", max_content_length=500)
    args = captured["args"]
    idx = args.index("--max-content-length")
    assert args[idx + 1] == "500"    # 合理值不被改动


def test_cass_search_final_still_too_large_returns_error(monkeypatch):
    """对照：即便砍回 user_limit，若最终 payload 仍超 256KB（如 max_content_length 巨大），
    须正当返回 result_too_large，不静默放行超契约体量。"""
    monkeypatch.setenv("CASS_MCP_BEARER", "0" * 64)
    from cass_mcp import server, runner
    monkeypatch.setattr(server, "_readiness", lambda: {"semantic": True, "lexical": True, "infinity": True})

    big_content = "c" * 100_000   # 5 条 * 100KB ≈ 500KB，砍不掉这个体量（无 hits 数量可砍）
    hits = [{"source_path": f"conv-{i}", "agent": "claude_code", "score": 1.0 - i * 0.01,
             "snippet": "s" * 100, "content": big_content} for i in range(5)]

    def fake_run_cass(subcmd, args, *, want_json=True, cass_bin=None, timeout_s=30,
                       max_bytes=262144, breaker=None, _now=None, oversize_is_failure=True):
        return {"hits": hits, "count": len(hits), "limit": 5,
                "total_matches": 5, "hits_clamped": False}

    monkeypatch.setattr(runner, "run_cass", fake_run_cass)

    result = server.cass_search("some query", limit=5)

    assert result.get("error") == "result_too_large"
    assert "hint" in result


def test_call_default_max_bytes_none_other_tools_unchanged(monkeypatch):
    """其他工具经 _call 不传 max_bytes/oversize_is_failure → run_cass 收到自身默认值
    （回归：其他工具字节级行为不变，_call 的 None 默认不会被转发覆盖 run_cass 自身默认）。"""
    monkeypatch.setenv("CASS_MCP_BEARER", "0" * 64)
    from cass_mcp import server, runner
    captured = {}
    _NOT_PASSED = object()   # 哨兵：区分「run_cass 自身默认」vs「_call 显式传入」

    def fake_run_cass(subcmd, args, *, want_json=True, cass_bin=None, timeout_s=30,
                       max_bytes=262144, breaker=None, _now=None, oversize_is_failure=_NOT_PASSED):
        captured["max_bytes"] = max_bytes
        captured["oversize_is_failure"] = oversize_is_failure
        return {"text": "ok"}

    monkeypatch.setattr(runner, "run_cass", fake_run_cass)
    server._call("cass_export", ["/p"])
    assert captured["max_bytes"] == 262144
    assert captured["oversize_is_failure"] is _NOT_PASSED   # _call 未转发，run_cass 用了自己的默认(True)
