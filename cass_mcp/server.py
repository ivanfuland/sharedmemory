# cass_mcp/server.py
import hashlib, json, os, time, urllib.request
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier   # B0 auth_api.md 确认 fastmcp 3.4.2 可用
from fastmcp.server.dependencies import get_access_token
from cass_mcp import runner, contract, config                       # config import 即设语义 env 默认
from cass_mcp.diversify import overfetch_limit, apply_search_postprocess
from cass_mcp._mcp_sdk_patch import apply_mcp_handle_message_patch

# mcp SDK #2064 运行时补丁(版本钉死,不符则跳过+CRITICAL;canonical 源在
# everos_mcp/server.py,两份同升同删)。import 时改写进程全局 Server 类——
# 生产是专属 systemd 进程,有意为之;会泄漏进 import 本模块的 pytest 进程,
# 是 import-time 全局补丁的固有代价,非缺陷。
apply_mcp_handle_message_patch()

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
    """P1-3: 查询前就绪校验（契约 cass-semantic-prod.md §20/§50）。
    改读 `cass status --json`（权威自述）取代旧的盲读文件/目录存在性检查——
    波3 起向量域迁入 DB vec0（vector_index/semantic_manifest.json 不复存在），
    波2 起词法域迁入 DB FTS5（index/ 目录不复存在），旧检查在新世界恒为 False。
    semantic 门用顶层 db_vector_domain 段；lexical 门用顶层 index 段——
    该段虽名为 index，但由内部 lexical asset 状态填充（实测 reason 文案含
    "lexical Tantivy metadata"，源码 state_index_freshness() 亦从 "index" 键
    读取 lexical 字段），是词法域迁库后仍存在的权威自述键，取代已消失的
    index/ 目录判断。"""
    checks = {"semantic": False, "lexical": False, "semantic_audit_status": None}
    try:
        st = runner.run_cass("status", [], want_json=True, cass_bin=config.CASS_BIN)
    except Exception:
        st = {"error": "cass_exception"}
    if isinstance(st, dict) and "error" not in st:
        dvd = st.get("db_vector_domain")
        if isinstance(dvd, dict):
            checks["semantic_audit_status"] = dvd.get("audit_status")
            embedder = dvd.get("embedder_id") or ""
            # R1-B3: audit_status 不作为门（代际 pending 期间搜索仍服务上一通过版），
            # 只观测透出；error 非 null 时其它字段不可信（R1-N7），一律判 False。
            checks["semantic"] = (
                dvd.get("error") is None
                and dvd.get("active") is True
                and isinstance(embedder, str) and embedder.endswith("bge-m3")
            )
        # dvd 为 None/缺失（老二进制无此字段，或 db 未打开）→ semantic 保持 False，不抛
        idx = st.get("index")
        if isinstance(idx, dict):
            checks["lexical"] = idx.get("exists") is True and idx.get("status") != "error"
    try:
        url = os.environ.get("CASS_INFINITY_URL", "").rstrip("/") + "/health"
        with urllib.request.urlopen(url, timeout=2) as r:
            checks["infinity"] = (getattr(r, "status", r.getcode()) == 200)
    except Exception:
        checks["infinity"] = False
    return checks

def _data_ready():
    """轻量 DB 就绪检查：仅校验 canonical DB 文件存在（pack/sessions/timeline 前置门）。
    不检查 Infinity/语义索引——那些由 _readiness() 负责（search 专属）。
    lexical index 缺失等边角让 cass_exit 兜，不过度。
    """
    dd = os.environ.get("CASS_DATA_DIR", "")
    return {"db": os.path.isfile(os.path.join(dd, "agent_search.db"))}

def _call(tool, args, max_bytes=None, oversize_is_failure=None):
    spec = contract.TOOLS[tool]                          # subcmd + want_json 单一来源
    t0 = time.monotonic()
    r = None
    try:
        extra = {}
        if max_bytes is not None: extra["max_bytes"] = max_bytes
        if oversize_is_failure is not None: extra["oversize_is_failure"] = oversize_is_failure
        r = runner.run_cass(spec["subcmd"], args, want_json=spec["want_json"], cass_bin=config.CASS_BIN, breaker=_BREAKER, **extra)
        return r
    except Exception as e:                               # cass_bin 缺失等：MCP 工具返回 error，不穿透
        r = {"error": "cass_exception", "detail": str(e)[:300]}
        return r
    finally:
        status = "error" if (r is None or "error" in r) else "ok"
        _audit(tool, args, status, round((time.monotonic() - t0) * 1000))

_MAX_CONTENT_LENGTH = 4000            # clamp 上限（默认 2000 的 2×）：bound 每条 hit 大小 → bound raw stdout
_SEARCH_RAW_MAX_BYTES = 8 * 1024 * 1024   # 8MB raw over-fetch parse 上限（对齐 cass_export preflight）。
# 经验界（非 CASS 源码推导，snippet/meta 尺寸是黑盒 CLI 行为，留大余量兜底）：
#   max_content_length≤4000 → 每条 content≤4000 字符(中文 UTF-8 ~3B/字≈12KB)+snippet+meta ≈≤25KB；
#   over-fetch≤150 条 → raw worst-case ≈3.75MB << 8MB（cap 实际可容 ~53KB/条，2× 余量）⇒ realistic 输入下 raw 恒 parse 成功 ⇒ min(L,N) 成立。
# 最终响应(≤user_limit 条)再按 runner.DEFAULT_MAX_BYTES(256KB) 复检（黑盒尺寸兜底闸）。

CASS_SEARCH_DESC = (
    "语义搜索跨 agent 历史会话（概念/语义召回，不靠关键词字面匹配）。当用户引用早先对话、"
    "说『之前/上次/我们讨论过』、或问某事来龙去脉时用。返回 hits[]，每条含 source_path/agent/snippet/score；"
    "要更全上下文用 cass_expand。新鲜度=每日 pull，最新对话可能尚未入索引。"
    "结果按会话多样化（单会话最多 3 条）；limit 上限 50，超出按 50 处理；max_content_length 上限 4000，超出按 4000。"
)

@mcp.tool(description=CASS_SEARCH_DESC)
def cass_search(query: str, agent: str = "", workspace: str = "", limit: int = 10,
                max_content_length: int = 2000):
    # P1-3: 查询前就绪校验（gate 只看三道布尔门；semantic_audit_status 是观测字段，不参与 all()）
    checks = _readiness()
    if not (checks["semantic"] and checks["lexical"] and checks["infinity"]):
        _audit("cass_search", [query], "not_ready", 0)
        return {"error": "not_ready", "checks": checks}
    user_limit, overfetch = overfetch_limit(limit)          # ② clamp ≤50 + overfetch ≥ user_limit
    mcl = max(1, min(int(max_content_length), _MAX_CONTENT_LENGTH))   # clamp → raw 可证明有界
    # P1-1: --rerank 恒开（已并入 SEMANTIC_FLAGS，无可关参数）
    args = [query, *config.SEMANTIC_FLAGS]                   # --mode semantic --daemon --model bge-m3 --rerank（契约定）
    args += ["--max-content-length", str(mcl), "--limit", str(overfetch)]  # 保 snippet，不 --fields minimal
    if agent: args += ["--agent", agent]
    if workspace: args += ["--workspace", workspace]
    r = _call("cass_search", args, max_bytes=_SEARCH_RAW_MAX_BYTES, oversize_is_failure=False)  # raw 恒能 parse；极端超限也不连累熔断
    r = apply_search_postprocess(r, user_limit)                       # 多样化 + 砍回 user_limit
    # 最终响应（≤user_limit 条）再按真正的 256KB MCP 契约复检；只有极端 max_content_length 才触
    if isinstance(r, dict) and isinstance(r.get("hits"), list):
        if len(json.dumps(r, ensure_ascii=False).encode("utf-8")) > runner.DEFAULT_MAX_BYTES:
            return {"error": "result_too_large", "hint": "narrow query: lower limit or max_content_length"}
    return r

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

@mcp.tool(description="把某主题的历史会话打包成确定性 answer pack（agent handoff/取证）。"
                      "注意：pack 的 semantic 模式实测不可用（cass 返回 semantic-unavailable code 15）；"
                      "pack 走默认 hybrid-preferred，实际 fail-open 到 lexical 证据——不是语义检索。"
                      "概念/中文模糊召回请用 cass_search（真语义）；pack 适合已知关键词时拿确定性证据包。返回 cass.pack.v1。")
def cass_pack(query: str, agent: str = "", workspace: str = "", limit: int = 10, max_tokens: int = 0):
    checks = _data_ready()
    if not all(checks.values()):
        _audit("cass_pack", [query], "not_ready", 0)
        return {"error": "not_ready", "checks": checks}
    args = [query, "--limit", str(limit)]            # ❗不加 SEMANTIC_FLAGS（pack 不支持）
    if max_tokens: args += ["--max-tokens", str(max_tokens)]
    if agent: args += ["--agent", agent]
    if workspace: args += ["--workspace", workspace]
    return _call("cass_pack", args)

@mcp.tool(description="列出最近会话（按时间倒序）。问『我最近在搞啥/某项目有哪些会话』时用。"
                      "返回 dict，sessions 字段是 list，每项 path/agent/title/message_count 等；要看内容用 cass_search/cass_expand。")
def cass_sessions(limit: int = 10, workspace: str = "", current: bool = False):
    checks = _data_ready()
    if not all(checks.values()):
        _audit("cass_sessions", ["--limit"], "not_ready", 0)
        return {"error": "not_ready", "checks": checks}
    args = ["--limit", str(limit)]
    if workspace: args += ["--workspace", workspace]
    if current: args.append("--current")
    return _call("cass_sessions", args)

@mcp.tool(description="某时间段的活动时间轴。问『上周二/最近三天干了啥』这类时间维度查询时用。"
                      "since 接受 today/yesterday/Nd(如 7d)/ISO 日期。")
def cass_timeline(since: str = "7d", until: str = "", agent: str = ""):
    checks = _data_ready()
    if not all(checks.values()):
        _audit("cass_timeline", [since], "not_ready", 0)
        return {"error": "not_ready", "checks": checks}
    args = ["--since", since]
    if until: args += ["--until", until]
    if agent: args += ["--agent", agent]
    return _call("cass_timeline", args)

if __name__ == "__main__":        # bearer 已在模块加载强制
    # stateless_http=True:fastmcp 3.4.2 默认有状态模式下,断开的客户端 session
    # 永不清理(底层 StreamableHTTPSessionManager 仅在 session_idle_timeout 配置
    # 时清理,而 fastmcp 未透传该参数)——每次客户端重连都在事件循环里攒一个
    # 卡在 app.run() 的僵尸协程,累计后 _session_creation_lock 永久阻塞,新连接
    # 全部超时(everos-mcp 实装期 session-churn 压测实证,2026-07-18)。本服务
    # 全部工具都是纯 request/response 检索(同步 def,零 session 状态依赖),
    # stateless 语义无损,且让"session 永不清理"的泄漏资源类别不存在。
    mcp.run(transport="http", host="127.0.0.1", port=config.CASS_PORT, stateless_http=True)
