# distill/writer.py
import json, os, re, urllib.request, urllib.error
from distill import idempotency

class McpError(Exception): pass
class PreWriteError(Exception): pass   # 写前(search/get)失败 → 留 pending 重试（R4 P1-1）

_TOKEN_FILE = os.path.expanduser("~/.config/gbrain/hub-bridge.token")
_SEARCH_RE = re.compile(r"^\[(?P<score>[-0-9.]+)\]\s+(?P<slug>\S+)\s+--\s+(?P<snip>.*?)(?P<stale>\s+\(stale\))?$")

def _post_raw(url, data, headers, timeout=60):
    """HTTP POST; returns decoded body string (SSE envelope included)."""
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()

def _parse_sse(body):
    """Parse SSE text/event-stream envelope → first JSON data object.
    gbrain returns 'event: message\\ndata: {...}\\n\\n'; falls back to plain JSON."""
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(body)   # fallback: plain JSON (future-proof)

def _post(url, data, headers, timeout=60):
    return _parse_sse(_post_raw(url, data, headers, timeout))

def mint_token(cfg):
    import urllib.parse
    cid = os.environ["HUB_BRIDGE_CLIENT_ID"]; sec = os.environ["HUB_BRIDGE_CLIENT_SECRET"]
    data = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "client_id": cid, "client_secret": sec}).encode()  # M2 token-refresh.sh 实证 form-encoded（P1-2）
    req = urllib.request.Request(cfg["gbrain"]["token_url"], data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]

def load_token(cfg):
    if os.path.exists(_TOKEN_FILE):
        t = open(_TOKEN_FILE).read().strip()
        if t: return t
    return mint_token(cfg)

def mcp_call(cfg, token, tool, args, timeout=60):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    out = _post(cfg["gbrain"]["mcp_url"], payload, {"Authorization": f"Bearer {token}"}, timeout)
    if "error" in out:
        raise McpError(f"{tool}: {out['error']}")
    res = out.get("result", {})
    # MCP content → 取首个 text 块解析（gbrain 文本优先）
    content = res.get("content")
    inner_text = ""
    if isinstance(content, list) and content and content[0].get("type") == "text":
        inner_text = content[0]["text"]
    if res.get("isError"):
        raise McpError(f"{tool} isError: {inner_text or res}")
    if inner_text:
        try: return json.loads(inner_text)
        except Exception: return {"text": inner_text}
    return res

def probe_tools(cfg, token):
    out = _post(cfg["gbrain"]["mcp_url"],
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                {"Authorization": f"Bearer {token}"})
    return {t["name"] for t in out.get("result", {}).get("tools", [])}

def entry_text(fact_text, source_ref, key):
    return f"{fact_text} （来源：{source_ref}）{idempotency.key_marker(key)}"

def page_markdown(name, kind, aliases, sources, date):
    al = "[" + ", ".join(aliases) + "]"
    src = "\n".join(f"  - {s}" for s in sources)
    alias_body = ("\n别名：" + "、".join(aliases) + " <!-- alias-mirror -->\n") if aliases else ""   # 入 body 供 search；带 stub 标记，_has_compiled_truth 不当真理（R3 P1-2）
    return (f"---\ntitle: {name}\ntype: {kind}\naliases: {al}\n"
            f"created_by: distill-bridge\ncreated: {date}\nsources:\n{src}\n---\n\n"
            f"# {name}\n{alias_body}\n（蒸馏桥自动创建；compiled truth 由 dream cycle 生成）\n")

def search_slugs(cfg, token, name, _call=None):
    call = _call or mcp_call
    out = call(cfg, token, "search", {"query": name})
    slugs = []
    for line in (out.get("text", "") if isinstance(out, dict) else "").splitlines():
        m = _SEARCH_RE.match(line.strip())
        if m: slugs.append((m["slug"], bool(m["stale"])))
    return slugs

def _queue_review(cfg, jrow, reason, hits):
    d = cfg["paths"]["review_queue"]; os.makedirs(d, exist_ok=True)
    fn = os.path.join(d, f"{jrow['key'][:16]}.json")
    json.dump({"reason": reason, "jrow": jrow, "hits": hits}, open(fn, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def _get_page_md(cfg, token, slug, call) -> "str|None":
    """Returns page markdown string if page exists, None if not-found. Other errors re-raise."""
    try:
        out = call(cfg, token, "get_page", {"slug": slug})
    except McpError as e:
        m = str(e).lower()
        if "not found" in m or "no such" in m or "404" in m or "does not exist" in m or "page_not_found" in m:
            return None                        # 仅"明确不存在"→允许建页（R3 P0-1）；live probe 确认信号含 "not found"（page_not_found）
        raise                                  # 传输/auth/server 错 → 上抛 → reconcile quarantine，绝不 put_page 覆写
    md = out.get("text", "") if isinstance(out, dict) else str(out)
    return md                                  # 空文本=不存在（gbrain 对缺页返回 isError+page_not_found McpError，上面处理）

def write_entry(cfg, token, conn, jrow, _call=None):
    """spec §2.5.2 search-before-write + §2.6 append-only。返回 done_new/done_append/review_queued。
    R2 P0-1：建页前 get_page 精确探针，已存在只 append 不 put_page。R4 P1-1：写前(search/get)失败→PreWriteError(留 pending 重试)，写后(put/timeline)失败→McpError(reconcile quarantine)。
    live probe 确认：timeline_add → add_timeline_entry（summary 字段），entry_text 写入 summary；
    SSE envelope 自动由 mcp_call 内部处理（_call mock 绕过网络层，不受影响）。"""
    call = _call or mcp_call
    slug = jrow["entity_slug"]; name = slug.split("/", 1)[1]
    try:                                          # —— 写前阶段：search + 精确存在探针 ——
        others = [h for h, _ in search_slugs(cfg, token, name, _call=call) if h != slug]
        md = None if others else _get_page_md(cfg, token, slug, call)
    except (McpError, OSError, ValueError) as e:
        raise PreWriteError(str(e)) from e        # 写前失败 → 不污染 journal，留 pending 下批重试（R4 P1-1）
    if others:                                    # 同名命中别的页（含 alias body-mirror）→ 人工 review，绝不自动选/建（P0-6）
        _queue_review(cfg, jrow, "ambiguous entity match", others)
        return "review_queued"
    existing = md is not None and bool(md.strip())
    created = False
    if not existing:                             # 仅当确认不存在才建页（不覆写既有 body/frontmatter）
        call(cfg, token, "put_page", {"slug": slug,
             "content": page_markdown(name, slug.split("/", 1)[0], [name], [jrow["source_ref"]], jrow["entry_date"])})
        created = True
    # --- Task 7 接线（codex R0 兼容 done_new/done_append）：替换 write_entry 内 `et = entry_text(...)` 那一行 ---
    from distill import stale
    flag = ""
    if existing:
        try:
            if stale.assess_contradiction(cfg, token, slug, jrow["fact_text"], call, page_md=md):
                flag = stale.CONTRADICTS_FLAG + " "
        except Exception:
            flag = ""   # advisory, judge failure does not block/quarantine valid write
    if flag or stale.is_high_impact(slug):
        _queue_review(cfg, jrow, "high-impact or contradiction", [slug])
    et = flag + entry_text(jrow["fact_text"], jrow["source_ref"], jrow["key"])
    # live probe: real tool name is add_timeline_entry, entry field is "summary" (probe-confirmed 2026-06-24)
    call(cfg, token, "add_timeline_entry", {"slug": slug, "date": jrow["entry_date"], "summary": et})   # 写后失败 → McpError 上抛 → reconcile quarantine
    conn.execute("UPDATE journal SET status='done' WHERE key=? AND status='pending'", (jrow["key"],))
    conn.commit()
    return "done_new" if created else "done_append"
