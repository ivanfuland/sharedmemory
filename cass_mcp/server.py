# cass_mcp/server.py
import json, os, time
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier   # B0 auth_api.md 确认 fastmcp 3.4.2 可用
from fastmcp.server.dependencies import get_access_token
from cass_mcp import runner, contract, config                       # config import 即设语义 env 默认

# bearer fail-fast：模块加载即强制 CASS_MCP_BEARER，缺失即 raise——覆盖 import/ASGI/main 全路径，杜绝 fail-open
_BEARER = os.environ.get("CASS_MCP_BEARER")
if not _BEARER:
    raise RuntimeError("CASS_MCP_BEARER 未设置：cass-mcp 拒绝无鉴权（fail-fast）")
_TOKENS = {_BEARER: {"client_id": os.environ.get("CASS_MCP_TOKEN_ID", "hub")}}
mcp = FastMCP("cass-mcp", auth=StaticTokenVerifier(_TOKENS))
_BREAKER = runner.CircuitBreaker()

def _token_id():
    try:
        tok = get_access_token()
        return getattr(tok, "client_id", None) or "?"
    except Exception:
        return "?"

def _audit(tool, params, status, dur_ms):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "params": str(params)[:200],
           "token_id": _token_id(), "status": status, "duration_ms": dur_ms}
    audit_path = os.environ.get("CASS_MCP_AUDIT", config.CASS_AUDIT)
    os.makedirs(os.path.dirname(os.path.abspath(audit_path)), exist_ok=True)
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def _call(tool, args):
    spec = contract.TOOLS[tool]                          # subcmd + want_json 单一来源
    t0 = time.monotonic()
    r = runner.run_cass(spec["subcmd"], args, want_json=spec["want_json"], cass_bin=config.CASS_BIN, breaker=_BREAKER)
    _audit(tool, args, "error" if "error" in r else "ok", round((time.monotonic() - t0) * 1000))
    return r

@mcp.tool(description="语义搜索跨 agent 历史会话（概念/语义召回，不靠关键词字面匹配）。当用户引用早先对话、"
                      "说『之前/上次/我们讨论过』、或问某事来龙去脉时用。返回 hits[]，每条含 source_path/agent/snippet/score；"
                      "要更全上下文用 cass_expand。新鲜度=每日 pull，最新对话可能尚未入索引。")
def cass_search(query: str, agent: str = "", workspace: str = "", limit: int = 10,
                max_content_length: int = 2000, rerank: bool = True):
    args = [query, *config.SEMANTIC_FLAGS]                          # --mode semantic --daemon --model bge-m3（契约定）
    if rerank: args.append("--rerank")                             # 默认开（spike rerank@5≈0.97）
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
        return {"error": "stat_failed", "detail": str(e)}
    if sz > _EXPORT_MAX_BYTES:
        return {"error": "session_too_large", "size_bytes": sz}
    return _call("cass_export", [source_path, "--format", fmt])

@mcp.tool(description="CASS 自检：索引新鲜度、库统计。注意：返回里的 semantic.available 反映的是 cass 原生 ONNX 路径"
                      "（本部署走本地 Infinity，故该字段为 false 属正常，不代表语义不可用）。")
def cass_triage(stale_threshold: int = 300):
    return _call("cass_triage", ["--stale-threshold", str(stale_threshold)])

if __name__ == "__main__":        # bearer 已在模块加载强制
    mcp.run(transport="http", host="127.0.0.1", port=config.CASS_PORT)
