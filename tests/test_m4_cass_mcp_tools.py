# tests/test_m4_cass_mcp_tools.py（B1 契约断言）
import json
import pathlib

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "cass-cli"


def test_cass_search_fixture_shape():
    """search fixture 语义模式输出：dict，hits 为非空 list，每条含 source_path 或 agent。"""
    d = json.loads((FIXTURES / "search.json").read_text())
    # search --json 返回 dict，hits key（非 results/rows）
    rows = d if isinstance(d, list) else d.get("hits", d.get("results", []))
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
    assert text.lstrip().startswith("#")


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


# ---- B3 追加 ----
# NOTE: fastmcp 3.4.2 的 @mcp.tool(description=...) 装饰器返回原始函数本身（非 FunctionTool），
# 因此无 .fn 属性。工具函数是普通 def，直接调用即可（无需 await）。
# FunctionTool 对象只能通过 `await mcp.get_tool(name)` 获取（用于元数据场景）。
import os, sys, subprocess
os.environ.setdefault("CASS_MCP_BEARER", "test-bearer")   # server 模块加载即要求 bearer（fail-fast），import 前置
from cass_mcp import server as S


def test_cass_search_tool_calls_runner(monkeypatch, tmp_path):
    calls = {}
    def fake_run(subcmd, args, **kw): calls["call"] = (subcmd, args, kw); return {"hits": [{"agent": "codex"}]}
    monkeypatch.setattr(S.runner, "run_cass", fake_run)
    monkeypatch.setattr(S, "_readiness", lambda: {"semantic": True, "lexical": True, "infinity": True})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "audit.log"))
    out = S.cass_search(query="x方案", limit=5)   # @mcp.tool 返回原函数，直接调用（非 .fn）
    assert out["hits"][0]["agent"] == "codex"
    args = calls["call"][1]
    assert calls["call"][0] == "search"
    # 语义 flags 必须在（读侧改走语义的硬纪律，契约 cass-semantic-prod.md）
    assert "--mode" in args and "semantic" in args and "--model" in args and "bge-m3" in args
    assert "--rerank" in args                                  # 恒开 rerank（P1-1: 并入 SEMANTIC_FLAGS，不可关）
    assert "--max-content-length" in args                      # 控量（非 --fields minimal，保 snippet）
    assert calls["call"][2].get("want_json") is True           # contract 驱动
    assert (tmp_path / "audit.log").exists()                   # 访问日志写了


def test_cass_search_no_rerank_param():
    """P1-1: cass_search 签名不再接受 rerank 参数（--rerank 恒开，无可关 footgun）。"""
    import inspect
    sig = inspect.signature(S.cass_search)
    assert "rerank" not in sig.parameters, "cass_search 不应暴露 rerank 参数（已并入 SEMANTIC_FLAGS 恒开）"


def test_cass_export_uses_text_mode(monkeypatch, tmp_path):
    small = tmp_path / "s.jsonl"; small.write_text("{}")
    calls = {}
    def fake_run(subcmd, args, **kw): calls["kw"] = kw; return {"text": "# md"}
    monkeypatch.setattr(S.runner, "run_cass", fake_run)
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "a.log"))
    out = S.cass_export(source_path=str(small))    # 直接调用，无需 await
    assert out["text"] == "# md" and calls["kw"].get("want_json") is False


def test_cass_export_rejects_oversized(tmp_path):
    big = tmp_path / "big.jsonl"; big.write_bytes(b"x" * (S._EXPORT_MAX_BYTES + 1))
    out = S.cass_export(source_path=str(big))      # 直接调用，无需 await
    assert out["error"] == "session_too_large"


def test_call_audits_and_wraps_runner_exception(monkeypatch, tmp_path):
    def boom(*a, **k): raise FileNotFoundError("no cass")
    monkeypatch.setattr(S.runner, "run_cass", boom)
    monkeypatch.setattr(S, "_readiness", lambda: {"semantic": True, "lexical": True, "infinity": True})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "a.log"))
    out = S.cass_search(query="q")
    assert out["error"] == "cass_exception"            # 异常转 error dict，不穿透
    assert (tmp_path / "a.log").read_text().strip()    # 审计写了


def test_cass_search_blocks_when_not_ready(monkeypatch, tmp_path):
    """P1-3: readiness 校验失败时 cass_search 返回 not_ready 且不调 run_cass。"""
    monkeypatch.setattr(S, "_readiness", lambda: {"semantic": False, "lexical": True, "infinity": True})
    called = {"n": 0}
    monkeypatch.setattr(S.runner, "run_cass", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "a.log"))
    out = S.cass_search(query="q")
    assert out["error"] == "not_ready"
    assert "checks" in out
    assert called["n"] == 0, "readiness 失败时不应调用 run_cass"


def test_server_module_refuses_import_without_bearer():
    env = {k: v for k, v in os.environ.items() if k != "CASS_MCP_BEARER"}
    r = subprocess.run([sys.executable, "-c", "import cass_mcp.server"],
                       env=env, capture_output=True, cwd=str(pathlib.Path(__file__).resolve().parent.parent))
    assert r.returncode != 0 and b"CASS_MCP_BEARER" in r.stderr
