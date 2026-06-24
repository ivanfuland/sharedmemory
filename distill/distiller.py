# distill/distiller.py
import json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "infra", "distill"))
from audit import audit_append  # R5：原文出机审计
from distill import idempotency

DISTILL_SCHEMA = {
    "name": "distill_extract",
    "schema": {
        "type": "object", "additionalProperties": False, "required": ["candidates"],
        "properties": {"candidates": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["entity_name", "entity_kind", "entry_type", "fact_text", "source_idx"],
            "properties": {
                "entity_name": {"type": "string"},
                "entity_kind": {"type": "string", "enum": ["person", "project", "decision", "preference"]},
                "entry_type": {"type": "string", "enum": ["fact", "decision", "lesson", "action_item"]},
                "fact_text": {"type": "string"},
                "source_idx": {"type": "integer"},
            }}}},
    },
    "strict": True,
}

_SYSTEM = (
    "你是个人记忆蒸馏器。从会话片段中抽取值得长期记住的世界知识：实体（人/项目）、"
    "事实、决策、踩坑教训(lesson)、待办(action_item)。严格按 schema 输出 candidates。\n"
    "规则：\n"
    "1) 每条 candidate 必须能回指原文——source_idx 填该结论所依据消息的 idx（必须是输入里出现过的 idx）。"
    "无法回指原文的不要编造，直接不输出。\n"
    "2) 跳过噪声：常规工具调用/文件读取输出、寒暄、无信息量的来回。\n"
    "3) entity_kind 选 person/project/decision/preference；entry_type 选 fact/decision/lesson/action_item。\n"
    "4) 没有任何值得记的 → candidates 输出空数组 []（不要编造）。"
)

def _render(rows):
    return "\n".join(f"[idx={r['idx']} {r['role']}] {r.get('content','')}" for r in rows)

def _chunk_rows(rows, chunk_size, overlap):
    """把 span 按 char 预算切块；单条超 chunk_size 的消息切多子块（同 idx 保 provenance），不丢中段（codex R0 P0-1）。"""
    chunks, cur, cur_len = [], [], 0
    def flush():
        nonlocal cur, cur_len
        if cur: chunks.append(cur); cur, cur_len = [], 0
    for r in rows:
        content = r.get("content", "") or ""
        if len(content) > chunk_size:
            flush()
            step = max(1, chunk_size - overlap)
            for i in range(0, len(content), step):
                chunks.append([{**r, "content": content[i:i + chunk_size]}])
            continue
        if cur_len + len(content) > chunk_size:
            flush()
        cur.append(r); cur_len += len(content)
    flush()
    return chunks

def _distill_one(chunk_rows, cfg, chat, session_ref):
    rendered = _render(chunk_rows)
    body = {"model": cfg["distill"]["model"], "temperature": 0,
            "response_format": {"type": "json_schema", "json_schema": DISTILL_SCHEMA},
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": rendered}]}
    last = None
    for _ in range(3):  # 1 + retry×2（spec §12.2）
        try:
            parsed = _validate(chat(body, cfg))
            audit_append(session_ref=session_ref, bytes_out=len(rendered.encode()),
                         model=cfg["distill"]["model"], path=cfg["paths"].get("audit_log"),
                         purpose="distill", status="ok")            # 每次成功出机审计（codex R1 P0-4）
            return parsed
        except Exception as e:
            audit_append(session_ref=session_ref, bytes_out=len(rendered.encode()),
                         model=cfg["distill"]["model"], path=cfg["paths"].get("audit_log"),
                         purpose="distill", status=f"error:{type(e).__name__}")   # 失败/重试也审计
            last = e
    raise last

def _chat_http(body, cfg):
    d = cfg["distill"]
    req = urllib.request.Request(d["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {d['api_key']}"})
    try:
        with urllib.request.urlopen(req, timeout=cfg["derived"]["distill_timeout_s"]) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        raise AssertionError(f"distill HTTP {e.code}: {e.read().decode()[:300]}")
    return json.loads(out["choices"][0]["message"]["content"])

def _validate(d):
    assert isinstance(d, dict) and set(d) == {"candidates"}, f"顶层非 strict: {set(d)}"
    for c in d["candidates"]:
        assert set(c) == {"entity_name", "entity_kind", "entry_type", "fact_text", "source_idx"}, f"candidate 非 strict: {c}"
    return d

def distill_span(rows, cfg, _chat=None):
    """长消息分块逐块蒸馏并候选（P0-1）；任一块 retry×2 仍败 → 抛 → 调用方标 raw_quarantined。每次出机由 _distill_one 审计（P0-4）。"""
    chat = _chat or _chat_http
    b = cfg["budget"]
    session_ref = rows[0].get("source_path", "?") if rows else "?"
    valid_idx = {r["idx"] for r in rows}
    kept, rejected = [], 0
    for chunk in _chunk_rows(rows, b["chunk_char_size"], b.get("chunk_overlap", 0)):
        parsed = _distill_one(chunk, cfg, chat, session_ref)
        for c in parsed["candidates"]:
            if c["source_idx"] in valid_idx:
                kept.append(c)
            else:
                rejected += 1  # 无法回指原文（spec §2.6 line 159-161）
    return {"candidates": kept, "rejected_no_provenance": rejected}

def build_journal_rows(candidates, raw_id, source_path):
    rows = []
    for c in candidates:
        slug = idempotency.slug_for(c["entity_kind"], c["entity_name"])
        src = f"{source_path}:{c['source_idx']}"
        key = idempotency.fact_key(src, slug, c["entry_type"], c["fact_text"])
        rows.append({"key": key, "raw_work_item_id": raw_id, "entity_slug": slug,
                     "entry_type": c["entry_type"], "fact_text": c["fact_text"], "source_ref": src})
    return rows

def commit_distilled(conn, raw_id, candidates, source_path):
    """spec §2.6.1 distill phase 事务边界（codex R0 P1-1）：单事务内 算 key→INSERT OR IGNORE journal→raw 标 distilled。"""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN")
    try:
        rows = build_journal_rows(candidates, raw_id, source_path)   # 纯计算，置于事务内满足"单事务"
        n = 0
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
                " VALUES(?,?,?,?,?,?,?, 'pending', ?)",
                (r["key"], r["raw_work_item_id"], r["entity_slug"], r["entry_type"],
                 r["fact_text"], r["source_ref"], now[:10], now))
            n += cur.rowcount
        upd = conn.execute("UPDATE raw_work_item SET status='distilled' WHERE id=? AND status='new'", (raw_id,))
        if upd.rowcount != 1:
            raise RuntimeError(f"distill commit: raw {raw_id} not in 'new' (affected={upd.rowcount})")
        conn.execute("COMMIT")
        return n
    except Exception:
        conn.execute("ROLLBACK"); raise
