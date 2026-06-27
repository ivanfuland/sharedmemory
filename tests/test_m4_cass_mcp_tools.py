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
    """contract.TOOLS 包含八个预期工具（原 5 + 新 3），且字段齐全。"""
    from cass_mcp.contract import TOOLS
    expected = {
        "cass_search", "cass_expand", "cass_context", "cass_export", "cass_triage",
        "cass_pack", "cass_sessions", "cass_timeline",
    }
    assert set(TOOLS.keys()) == expected, f"期望 8 工具，实际 {set(TOOLS.keys())}"
    assert len(TOOLS) == 8, f"contract.TOOLS 应有 8 条，实际 {len(TOOLS)}"
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


# ---- M4 Phase B 新增：cass_pack / cass_sessions / cass_timeline ----

def test_cass_sessions_fixture_shape():
    """sessions fixture：dict，含 sessions 列表，每条含 path/agent/title/message_count。"""
    d = json.loads((FIXTURES / "sessions.json").read_text())
    assert isinstance(d, dict), "sessions fixture 应为 dict"
    assert "sessions" in d, "顶层应有 sessions 键"
    rows = d["sessions"]
    assert isinstance(rows, list) and rows, "sessions 列表应非空"
    first = rows[0]
    for key in ("path", "agent", "title", "message_count"):
        assert key in first, f"sessions 每条应含 {key}"


def test_cass_timeline_fixture_shape():
    """timeline fixture：dict，含 range/total_sessions/groups。"""
    d = json.loads((FIXTURES / "timeline.json").read_text())
    assert isinstance(d, dict), "timeline fixture 应为 dict"
    assert "range" in d
    assert "total_sessions" in d
    assert "groups" in d


def test_cass_pack_fixture_shape():
    """pack fixture：dict，schema_version=cass.pack.v1，不含语义 flag 痕迹。"""
    d = json.loads((FIXTURES / "pack.json").read_text())
    assert isinstance(d, dict), "pack fixture 应为 dict"
    assert d.get("schema_version") == "cass.pack.v1", "pack 应返回 cass.pack.v1"
    # 验证 fixture 是用纯 lexical 模式抓的（无语义字段污染）
    assert "query" in d


def test_cass_pack_tool_calls_runner_no_semantic_flags(monkeypatch, tmp_path):
    """cass_pack 走 run_cass subcmd=pack，args 含 query+--limit，
    且绝不含 --mode / semantic / --rerank（pack 不支持语义 flag）。"""
    calls = {}
    def fake_run(subcmd, args, **kw):
        calls["call"] = (subcmd, args, kw)
        return {"schema_version": "cass.pack.v1", "pack": []}
    monkeypatch.setattr(S.runner, "run_cass", fake_run)
    monkeypatch.setattr(S, "_data_ready", lambda: {"db": True})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "audit.log"))
    out = S.cass_pack(query="记忆", limit=5)
    assert out.get("schema_version") == "cass.pack.v1"
    subcmd, args, kw = calls["call"]
    assert subcmd == "pack"
    assert "记忆" in args, "query 应作为位置参数传入"
    assert "--limit" in args and "5" in args
    assert kw.get("want_json") is True, "pack want_json 应为 True"
    # 硬断言：pack 绝不含语义 flag（防回归）
    args_str = " ".join(str(a) for a in args)
    assert "--mode" not in args_str, "pack args 不应含 --mode"
    assert "semantic" not in args_str, "pack args 不应含 semantic"
    assert "--rerank" not in args_str, "pack args 不应含 --rerank"
    assert "--model" not in args_str, "pack args 不应含 --model"
    assert (tmp_path / "audit.log").exists(), "审计日志应写入"


def test_cass_pack_optional_args(monkeypatch, tmp_path):
    """cass_pack：max_tokens/agent/workspace 传了才加对应 flag。"""
    calls = {}
    def fake_run(subcmd, args, **kw):
        calls["call"] = (subcmd, args, kw)
        return {"schema_version": "cass.pack.v1", "pack": []}
    monkeypatch.setattr(S.runner, "run_cass", fake_run)
    monkeypatch.setattr(S, "_data_ready", lambda: {"db": True})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "a.log"))
    S.cass_pack(query="q", max_tokens=4000, agent="codex", workspace="/tmp")
    _, args, _ = calls["call"]
    assert "--max-tokens" in args and "4000" in args
    assert "--agent" in args and "codex" in args
    assert "--workspace" in args and "/tmp" in args


def test_cass_sessions_tool_calls_runner(monkeypatch, tmp_path):
    """cass_sessions：subcmd=sessions，args 含 --limit；DB readiness gate 须通过。"""
    calls = {}
    def fake_run(subcmd, args, **kw):
        calls["call"] = (subcmd, args, kw)
        return {"sessions": [{"path": "/x", "agent": "cc", "title": "T", "message_count": 1}]}
    monkeypatch.setattr(S.runner, "run_cass", fake_run)
    monkeypatch.setattr(S, "_data_ready", lambda: {"db": True})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "audit.log"))
    out = S.cass_sessions(limit=5)
    assert "sessions" in out
    subcmd, args, kw = calls["call"]
    assert subcmd == "sessions"
    assert "--limit" in args and "5" in args
    assert kw.get("want_json") is True
    assert (tmp_path / "audit.log").exists()


def test_cass_sessions_optional_workspace_current(monkeypatch, tmp_path):
    """workspace/current 传了才加对应 flag。"""
    calls = {}
    def fake_run(subcmd, args, **kw):
        calls["call"] = (subcmd, args, kw); return {"sessions": []}
    monkeypatch.setattr(S.runner, "run_cass", fake_run)
    monkeypatch.setattr(S, "_data_ready", lambda: {"db": True})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "a.log"))
    # workspace 传了 → 含 --workspace
    S.cass_sessions(workspace="/proj/x")
    _, args, _ = calls["call"]
    assert "--workspace" in args and "/proj/x" in args
    assert "--current" not in args
    # current=True → 含 --current
    S.cass_sessions(current=True)
    _, args, _ = calls["call"]
    assert "--current" in args


def test_cass_timeline_tool_calls_runner(monkeypatch, tmp_path):
    """cass_timeline：subcmd=timeline，args 含 --since；DB readiness gate 须通过，无 --week。"""
    calls = {}
    def fake_run(subcmd, args, **kw):
        calls["call"] = (subcmd, args, kw)
        return {"range": {}, "total_sessions": 0, "groups": []}
    monkeypatch.setattr(S.runner, "run_cass", fake_run)
    monkeypatch.setattr(S, "_data_ready", lambda: {"db": True})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "audit.log"))
    out = S.cass_timeline(since="7d")
    assert "groups" in out
    subcmd, args, kw = calls["call"]
    assert subcmd == "timeline"
    assert "--since" in args and "7d" in args
    assert kw.get("want_json") is True
    # 硬断言：绝不含 --week（该 flag 不存在会报错）
    assert "--week" not in args, "timeline 不应含 --week flag（该 flag 不存在）"
    assert (tmp_path / "audit.log").exists()


def test_cass_timeline_optional_until_agent(monkeypatch, tmp_path):
    """until/agent 传了才加对应 flag。"""
    calls = {}
    def fake_run(subcmd, args, **kw):
        calls["call"] = (subcmd, args, kw); return {"range": {}, "total_sessions": 0, "groups": []}
    monkeypatch.setattr(S.runner, "run_cass", fake_run)
    monkeypatch.setattr(S, "_data_ready", lambda: {"db": True})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "a.log"))
    S.cass_timeline(since="today", until="yesterday", agent="codex")
    _, args, _ = calls["call"]
    assert "--until" in args and "yesterday" in args
    assert "--agent" in args and "codex" in args


# ---- DB readiness gate：pack/sessions/timeline 当 DB 缺失时返回 not_ready ----

def test_cass_pack_not_ready_when_db_missing(monkeypatch, tmp_path):
    """P2: DB 缺失时 cass_pack 返回 not_ready，不调 run_cass。"""
    monkeypatch.setattr(S, "_data_ready", lambda: {"db": False})
    called = {"n": 0}
    monkeypatch.setattr(S.runner, "run_cass", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "a.log"))
    out = S.cass_pack(query="记忆")
    assert out["error"] == "not_ready", "DB 缺失时应返回 not_ready"
    assert "checks" in out
    assert out["checks"]["db"] is False
    assert called["n"] == 0, "not_ready 时不应调用 run_cass"


def test_cass_sessions_not_ready_when_db_missing(monkeypatch, tmp_path):
    """P2: DB 缺失时 cass_sessions 返回 not_ready，不调 run_cass。"""
    monkeypatch.setattr(S, "_data_ready", lambda: {"db": False})
    called = {"n": 0}
    monkeypatch.setattr(S.runner, "run_cass", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "a.log"))
    out = S.cass_sessions()
    assert out["error"] == "not_ready", "DB 缺失时应返回 not_ready"
    assert "checks" in out
    assert out["checks"]["db"] is False
    assert called["n"] == 0, "not_ready 时不应调用 run_cass"


def test_cass_timeline_not_ready_when_db_missing(monkeypatch, tmp_path):
    """P2: DB 缺失时 cass_timeline 返回 not_ready，不调 run_cass。"""
    monkeypatch.setattr(S, "_data_ready", lambda: {"db": False})
    called = {"n": 0}
    monkeypatch.setattr(S.runner, "run_cass", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {})
    monkeypatch.setenv("CASS_MCP_AUDIT", str(tmp_path / "a.log"))
    out = S.cass_timeline(since="7d")
    assert out["error"] == "not_ready", "DB 缺失时应返回 not_ready"
    assert "checks" in out
    assert out["checks"]["db"] is False
    assert called["n"] == 0, "not_ready 时不应调用 run_cass"
