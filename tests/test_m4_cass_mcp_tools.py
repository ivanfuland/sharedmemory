# tests/test_m4_cass_mcp_tools.py（B1 契约断言）
import json
import pathlib

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "cass-cli"


def test_cass_search_fixture_shape():
    """search fixture 语义模式输出：dict，hits 为非空 list，每条含 source_path 或 agent。"""
    d = json.loads((FIXTURES / "search.json").read_text())
    # search --json 返回 dict，hits key（非 results/rows）
    rows = d if isinstance(d, list) else d.get("results", d.get("hits", []))
    assert isinstance(rows, list)
    assert rows, "search fixture 应有结果（语义模式在真实语料上必有命中）"
    assert "source_path" in rows[0] or "agent" in rows[0]


def test_cass_expand_fixture_shape():
    """expand fixture：list，每条含 line/role/is_target/content。"""
    d = json.loads((FIXTURES / "expand.json").read_text())
    assert isinstance(d, list)
    assert d, "expand fixture 应非空"
    assert "line" in d[0]
    assert "role" in d[0]
    assert "is_target" in d[0]


def test_cass_context_fixture_shape():
    """context fixture：dict，含 source/related/counts 顶层 key。"""
    d = json.loads((FIXTURES / "context.json").read_text())
    assert isinstance(d, dict)
    assert "source" in d
    assert "related" in d


def test_cass_export_fixture_shape():
    """export fixture：plain text markdown（非 JSON），以 # 标题开头。"""
    text = (FIXTURES / "export.md").read_text()
    assert text.strip(), "export fixture 应非空"
    # markdown export 首行含 # 标题
    assert "#" in text


def test_cass_triage_fixture_shape():
    """triage fixture：dict，含 healthy/status/recommended_commands。"""
    d = json.loads((FIXTURES / "triage.json").read_text())
    assert isinstance(d, dict)
    assert "healthy" in d
    assert "status" in d
    assert "recommended_commands" in d


def test_contract_tools_keys():
    """contract.TOOLS 包含五个预期工具，且字段齐全。"""
    from cass_mcp.contract import TOOLS
    expected = {"cass_search", "cass_expand", "cass_context", "cass_export", "cass_triage"}
    assert set(TOOLS.keys()) == expected
    for name, cfg in TOOLS.items():
        assert "subcmd" in cfg, f"{name} 缺 subcmd"
        assert "want_json" in cfg, f"{name} 缺 want_json"
        assert "arg" in cfg, f"{name} 缺 arg"


def test_contract_search_hits_extractor():
    """contract.extract_search_hits 能从真实 fixture 取出 hits。"""
    from cass_mcp.contract import extract_search_hits
    d = json.loads((FIXTURES / "search.json").read_text())
    hits = extract_search_hits(d)
    assert isinstance(hits, list)
    assert hits, "hits 应非空"
    assert "source_path" in hits[0]
