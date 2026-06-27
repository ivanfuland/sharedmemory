# cass_mcp/contract.py —— 工具→子命令真契约（B1 probe 钉死，codex R0 P0-1/P2-3 单一来源，B3 必复用此处不另写默认）
# want_json=False（export）输出文本不是 JSON → runner 不加 --json、不 json.loads，返回 {"text":...}
#
# B1 probe 实测形态（2026-06-27，cass 0.6.17，canonical DB ~1717 sessions）：
#   search  → dict，顶层 keys: query/limit/offset/count/total_matches/hits/...
#             hits 列表（非 results/rows），每条含 source_path/agent/score/snippet 等
#   expand  → list，每条含 line/role/is_target/content
#   context → dict，顶层 keys: source/related/counts
#   export  → plain text markdown（无 --json flag，不 json.loads）
#   triage  → dict，顶层 keys: surface/schema_version/status/healthy/initialized/...
#
# 注意：search 的语义 flag（--mode semantic --daemon --model bge-m3 --rerank）
#       不在此处编码，由 B3 的 cass_search 构造 args 时加；contract 只管 subcmd/want_json/arg 映射。

TOOLS = {
    "cass_search":  {"subcmd": "search",  "want_json": True,  "arg": "query_positional"},
    # <QUERY> + 语义 flag + --max-content-length/--limit/--agent/--workspace
    # JSON shape: dict → d["hits"] is list; each hit has source_path/agent/score/snippet/content
    "cass_expand":  {"subcmd": "expand",  "want_json": True,  "arg": "path_positional"},
    # <PATH> --line N --context N（位置参数，实测 OK）
    # JSON shape: list; each item has line/role/is_target/content
    "cass_context": {"subcmd": "context", "want_json": True,  "arg": "path_positional"},
    # <PATH> --limit N（位置参数，非 --source）
    # JSON shape: dict → source / related / counts
    "cass_export":  {"subcmd": "export",  "want_json": False, "arg": "path_positional"},
    # <PATH> --format markdown → plain text（位置参数；runner 返回 {"text": ...}）
    "cass_triage":  {"subcmd": "triage",  "want_json": True,  "arg": "none"},
    # --stale-threshold N
    # JSON shape: dict → healthy / status / recommended_commands / search_completeness / ...
}

# 从 fixture 提取 search hits 的标准辅助（与 B3 runner 共享，避免重复硬编码 key 路径）
def extract_search_hits(data: dict) -> list:
    """从 cass search --json 的输出 dict 取 hits 列表。
    shape: data["hits"] = list[dict{source_path, agent, score, snippet, content, ...}]
    """
    return data.get("hits", [])
