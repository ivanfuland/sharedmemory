# cass_mcp/server.py
import hashlib, json, os, time, urllib.request
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier   # B0 auth_api.md 确认 fastmcp 3.4.2 可用
from fastmcp.server.dependencies import get_access_token
from cass_mcp import runner, contract, config                       # config import 即设语义 env 默认

# bearer fail-fast：模块加载即强制 CASS_MCP_BEARER，缺失即 raise——覆盖 import/ASGI/main 全路径，杜绝 fail-open
_BEARER = os.environ.get("CASS_MCP_BEARER")
if not _BEARER:
    raise RuntimeError("CASS_MCP_BEARER 未设置：cass-mcp 拒绝无鉴权（fail-fast）")
_TOKENS = {_BEARER: {"client_id": os.environ.get("CASS_MCP_TOKEN_ID", "hub"), "scopes": []}}
mcp = FastMCP("cass-mcp", auth=StaticTokenVerifier(_TOKENS))
_BREAKER = runner.CircuitBreaker()

def _token_id():
    try:
        tok = get_access_token()
        return getattr(tok, "client_id", None) or "?"
    except Exception:
        return "?"

def _audit(tool, params, status, dur_ms):
    # P2-2: 脱敏——不记原始 query 文本，只记长度 + sha256[:12]
    p = params if isinstance(params, list) else [params]
    q = p[0] if p else ""
    safe = {"argc": len(p), "q_len": len(str(q)), "q_sha12": hashlib.sha256(str(q).encode()).hexdigest()[:12]}
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "params": safe,
               "token_id": _token_id(), "status": status, "duration_ms": dur_ms}
        audit_path = os.environ.get("CASS_MCP_AUDIT", config.CASS_AUDIT)
        os.makedirs(os.path.dirname(os.path.abspath(audit_path)), exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 审计尽力而为，绝不掩盖工具返回


def _readiness():
    """P1-3: 查询前就绪校验（契约 cass-semantic-prod.md §20/§50）。"""
    dd = os.environ.get("CASS_DATA_DIR", "")
    checks = {}
    try:
        m = json.load(open(os.path.join(dd, "vector_index", "semantic_manifest.json")))["quality_tier"]
        checks["semantic"] = bool(m.get("ready")) and m.get("embedder_id") == "bge-m3"
    except Exception:
        checks["semantic"] = False
    checks["lexical"] = os.path.isdir(os.path.join(dd, "index"))
    try:
        url = os.environ.get("CASS_INFINITY_URL", "").rstrip("/") + "/health"
        with urllib.request.urlopen(url, timeout=2) as r:
            checks["infinity"] = (getattr(r, "status", r.getcode()) == 200)
    except Exception:
        checks["infinity"] = False
    return checks

def _call(tool, args):
    spec = contract.TOOLS[tool]                          # subcmd + want_json 单一来源
    t0 = time.monotonic()
    r = None
    try:
        r = runner.run_cass(spec["subcmd"], args, want_json=spec["want_json"], cass_bin=config.CASS_BIN, breaker=_BREAKER)
        return r
    except Exception as e:                               # cass_bin 缺失等：MCP 工具返回 error，不穿透
        r = {"error": "cass_exception", "detail": str(e)[:300]}
        return r
    finally:
        status = "error" if (r is None or "error" in r) else "ok"
        _audit(tool, args, status, round((time.monotonic() - t0) * 1000))

@mcp.tool(description="语义搜索跨 agent 历史会话（概念/语义召回，不靠关键词字面匹配）。当用户引用早先对话、"
                      "说『之前/上次/我们讨论过』、或问某事来龙去脉时用。返回 hits[]，每条含 source_path/agent/snippet/score；"
                      "要更全上下文用 cass_expand。新鲜度=每日 pull，最新对话可能尚未入索引。")
def cass_search(query: str, agent: str = "", workspace: str = "", limit: int = 10,
                max_content_length: int = 2000):
    # P1-3: 查询前就绪校验（语义 manifest + 词法 index + Infinity health）
    checks = _readiness()
    if not all(checks.values()):
        _audit("cass_search", [query], "not_ready", 0)
        return {"error": "not_ready", "checks": checks}
    # P1-1: --rerank 恒开（已并入 SEMANTIC_FLAGS，无可关参数）
    args = [query, *config.SEMANTIC_FLAGS]                          # --mode semantic --daemon --model bge-m3 --rerank（契约定）
    args += ["--max-content-length", str(max_content_length), "--limit", str(limit)]  # 保 snippet，不 --fields minimal
    if agent: args += ["--agent", agent]
    if workspace: args += ["--workspace", workspace]
    return _call("cass_search", args)

@mcp.tool(description="展开某会话片段上下文（拿 cass_search 的 source_path + line 后看前后消息）。")
def cass_expand(source_path: str, line: int, context: int = 3):
    return _call("cass_expand", [source_path, "--line", str(line), "--context", str(context)])

@mcp.tool(description="某会话的相关会话（跨 session 关联）。")
def cass_context(source_path: str, limit: int = 5):
    return _call("cass_context", [source_path, "--limit", str(limit)])

_EXPORT_MAX_BYTES = 8 * 1024 * 1024   # export preflight：source 文件超此即拒（实测有 74MB session，防 subprocess 全量缓冲）

@mcp.tool(description="导出整段会话为 markdown（审计/取证，量大慎用）。返回 {text}；session 文件 >8MB 拒绝。")
def cass_export(source_path: str, fmt: str = "markdown"):
    try:
        sz = os.path.getsize(source_path)
    except OSError as e:
        r = {"error": "stat_failed", "detail": str(e)}
        _audit("cass_export", [source_path], "error", 0)
        return r
    if sz > _EXPORT_MAX_BYTES:
        r = {"error": "session_too_large", "size_bytes": sz}
        _audit("cass_export", [source_path], "error", 0)
        return r
    return _call("cass_export", [source_path, "--format", fmt])

@mcp.tool(description="CASS 自检：索引新鲜度、库统计。注意：返回里的 semantic.available 反映的是 cass 原生 ONNX 路径"
                      "（本部署走本地 Infinity，故该字段为 false 属正常，不代表语义不可用）。")
def cass_triage(stale_threshold: int = 300):
    return _call("cass_triage", ["--stale-threshold", str(stale_threshold)])

if __name__ == "__main__":        # bearer 已在模块加载强制
    mcp.run(transport="http", host="127.0.0.1", port=config.CASS_PORT)
