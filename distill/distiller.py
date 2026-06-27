# distill/distiller.py
import json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    _TZ = ZoneInfo("Asia/Shanghai")          # 蒸馏日期统一 GMT+8（Ivan 本地"哪天聊的"语义）
except ZoneInfoNotFoundError:                # 极简环境缺系统 tzdata → 固定 +8（China 无 DST，对日期等价）
    _TZ = timezone(timedelta(hours=8))
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
    "你是个人记忆蒸馏器。从会话片段抽取值得长期记住的世界知识，按粒度规范输出 JSON。\n"
    "总原则=拆细：每条 candidate=一个独立可检索的事实/决策/偏好/教训/待办；宁拆细勿揉合；同一事实换措辞只记一条。\n"
    "1) 每条 candidate 必须回指原文：source_idx 填依据消息的 idx（必须是输入出现过的 idx）；无法回指的不编造、不输出。\n"
    "2) 跳过噪声：工具调用/文件读取输出、寒暄、无信息量来回 → candidates 为 []。\n"
    "3) entity_kind 选 person/project/decision/preference；entry_type 选 fact/decision/lesson/action_item。\n"
    "4) 粒度：R1 不同维度/槽位各一条；R2 每个独立可查的参数/取值各一条（配置/指标 bundle 按值拆）；R3 多实体各自角色各一条（按各自实体归属）；R4「用A不用B」拆2：{用A}+{不用B}；成就+多指标拆开（成就1条+每指标各1条）；关系只记一条（按被查端，不两端各记）；约束+理由同条（除非理由独立有用）。\n"
    "输出严格 JSON 对象（不要代码块、不要解释）：\n"
    '{"candidates":[{"entity_name":"<串>","entity_kind":"person|project|decision|preference","entry_type":"fact|decision|lesson|action_item","fact_text":"<串>","source_idx":<整数>}]}\n'
    "示例（学这个粒度）：\n"
    "输入：[idx=0 user] 老兰定 LFT：用 Qlib 不用 backtrader；前6月只模拟盘\n"
    "输出："
    '{"candidates":[{"entity_name":"LFT","entity_kind":"project","entry_type":"decision","fact_text":"底座用 Qlib","source_idx":0},'
    '{"entity_name":"LFT","entity_kind":"project","entry_type":"decision","fact_text":"不用 backtrader","source_idx":0},'
    '{"entity_name":"LFT","entity_kind":"project","entry_type":"decision","fact_text":"前6个月只跑模拟盘","source_idx":0}]}\n'
    "（含「用A不用B」拆2 + 独立维度各一条；纯噪声/寒暄→candidates 为 []。）"
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
    body = {"model": cfg["distill"]["model"], "temperature": float(os.environ.get("DISTILL_TEMP", "0")), "max_tokens": 4000,
            "response_format": {"type": "json_object"},
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

_ENTITY_KINDS = {"person", "project", "decision", "preference"}
_ENTRY_TYPES = {"fact", "decision", "lesson", "action_item"}
_CAND_KEYS = {"entity_name", "entity_kind", "entry_type", "fact_text", "source_idx"}

def _validate(d):
    assert isinstance(d, dict) and set(d) == {"candidates"}, f"顶层非 strict: {set(d) if isinstance(d, dict) else type(d)}"
    assert isinstance(d["candidates"], list), "candidates 非数组"
    for c in d["candidates"]:
        assert isinstance(c, dict) and set(c) == _CAND_KEYS, f"candidate 字段非 strict: {c}"
        assert isinstance(c["entity_name"], str) and c["entity_name"].strip(), "entity_name 空/非串"
        assert c["entity_kind"] in _ENTITY_KINDS, f"entity_kind 非法: {c.get('entity_kind')}"
        assert c["entry_type"] in _ENTRY_TYPES, f"entry_type 非法: {c.get('entry_type')}"
        assert isinstance(c["fact_text"], str) and c["fact_text"].strip(), "fact_text 空/非串"
        assert isinstance(c["source_idx"], int) and not isinstance(c["source_idx"], bool), "source_idx 非整数"
    return d

def _msg_date(ts):
    """CASS created_at（epoch 毫秒）→ 'YYYY-MM-DD'；缺失/异常 → None（commit 退回跑批日）。"""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000, _TZ).date().isoformat()
    except (ValueError, OSError, TypeError, OverflowError):
        return None

def distill_span(rows, cfg, _chat=None):
    """长消息分块逐块蒸馏并候选（P0-1）；任一块 retry×2 仍败 → 抛 → 调用方标 raw_quarantined。每次出机由 _distill_one 审计（P0-4）。"""
    chat = _chat or _chat_http
    b = cfg["budget"]
    session_ref = rows[0].get("source_path", "?") if rows else "?"
    valid_idx = {r["idx"] for r in rows}
    idx_date = {r["idx"]: _msg_date(r.get("created_at")) for r in rows}   # idx→会话真实日期(YYYY-MM-DD)
    kept, rejected = [], 0
    for chunk in _chunk_rows(rows, b["chunk_char_size"], b.get("chunk_overlap", 0)):
        parsed = _distill_one(chunk, cfg, chat, session_ref)
        for c in parsed["candidates"]:
            if c["source_idx"] in valid_idx:
                kept.append({**c, "entry_date": idx_date.get(c["source_idx"])})   # 盖会话日期非跑批日
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
                     "entry_type": c["entry_type"], "fact_text": c["fact_text"], "source_ref": src,
                     "entry_date": c.get("entry_date")})
    return rows

def commit_distilled(conn, raw_id, candidates, source_path):
    """spec §2.6.1 distill phase 事务边界（codex R0 P1-1）：单事务内 算 key→INSERT OR IGNORE journal→raw 标 distilled。"""
    now = datetime.now(timezone.utc).isoformat()
    today_local = datetime.now(_TZ).date().isoformat()   # 缺会话日期时的统一退回(GMT+8，循环外算一次防跨午夜混两天)
    conn.execute("BEGIN")
    try:
        rows = build_journal_rows(candidates, raw_id, source_path)   # 纯计算，置于事务内满足"单事务"
        n = 0
        for r in rows:
            entry_date = r.get("entry_date") or today_local   # 会话真实日期；缺失退回跑批日(GMT+8)
            cur = conn.execute(
                "INSERT OR IGNORE INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
                " VALUES(?,?,?,?,?,?,?, 'pending', ?)",
                (r["key"], r["raw_work_item_id"], r["entity_slug"], r["entry_type"],
                 r["fact_text"], r["source_ref"], entry_date, now))
            n += cur.rowcount
        upd = conn.execute("UPDATE raw_work_item SET status='distilled' WHERE id=? AND status='new'", (raw_id,))
        if upd.rowcount != 1:
            raise RuntimeError(f"distill commit: raw {raw_id} not in 'new' (affected={upd.rowcount})")
        conn.execute("COMMIT")
        return n
    except Exception:
        conn.execute("ROLLBACK"); raise
