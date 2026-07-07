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
