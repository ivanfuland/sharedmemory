# distill/cass_reader.py
import sqlite3, hashlib
from contextlib import closing
from datetime import datetime, timezone

class FingerprintMismatch(Exception): pass

# 规范化读 SQL（contracts/cass-canonical-fields.json read_sql）：JOIN + 全局游标 messages.id
_READ_SQL = """
SELECT m.id AS mid, m.conversation_id AS cid, m.idx AS idx, m.role AS role,
       m.created_at AS ts, m.content AS content,
       a.slug AS agent, w.path AS workspace,
       c.source_path AS source_path, c.id AS conv_id
FROM messages m
JOIN conversations c ON c.id = m.conversation_id
JOIN agents a ON a.id = c.agent_id
LEFT JOIN workspaces w ON w.id = c.workspace_id
WHERE m.id > :last AND a.slug = :agent AND (:ws = '' OR w.path = :ws)
ORDER BY m.id ASC
LIMIT :lim
"""

def _schema_fingerprint(canon_db):
    with closing(sqlite3.connect(f"file:{canon_db}?mode=ro", uri=True)) as db:
        parts = []
        for t in ("messages", "conversations", "agents", "workspaces"):
            cols = db.execute(f"PRAGMA table_info({t})").fetchall()
            parts.append(t + ":" + ",".join(sorted(f"{c[1]}/{c[2]}" for c in cols)))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()

def verify_fingerprint(canon_db, fingerprint_path):
    with open(fingerprint_path, encoding="utf-8") as f:
        want = f.read().strip()
    got = _schema_fingerprint(canon_db)
    # 契约文件格式：可为 "sha256" 或 "label sha256"；取末段比对
    if got != want.split()[-1]:
        raise FingerprintMismatch(f"CASS schema 指纹不符 want={want!r} got={got!r} — 拒绝运行(spec §2.2.1)")

def _cur(conn, source_id, workspace):
    r = conn.execute("SELECT stream_position FROM cursor WHERE source_id=? AND workspace=?", (source_id, workspace)).fetchone()
    return r[0] if r else 0

def read_spans(canon_db, conn, source_id, agent_slug, max_messages, workspace=""):
    """JOIN 读 id>cursor（可选 workspace 过滤，R2 P0-2）→ 按 conversation 聚 raw_work_item → 游标(per source_id+workspace)与 raw 同事务。"""
    with closing(sqlite3.connect(f"file:{canon_db}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        last = _cur(conn, source_id, workspace)
        rows = db.execute(_READ_SQL, {"last": last, "agent": agent_slug, "ws": workspace, "lim": max_messages}).fetchall()
    if not rows:
        return 0
    spans = {}  # conv_id -> [min_mid, max_mid, source_path]
    for r in rows:
        s = spans.setdefault(r["conv_id"], [r["mid"], r["mid"], r["source_path"]])
        s[0] = min(s[0], r["mid"]); s[1] = max(s[1], r["mid"])
    new_max = max(r["mid"] for r in rows)
    now = datetime.now(timezone.utc).isoformat()
    created = 0
    # 单事务：raw 落盘 + 游标推进原子提交（read 后崩溃零丢失，spec §2.6.1 read phase）
    conn.execute("BEGIN")
    try:
        for conv_id, (lo, hi, sp) in spans.items():
            cur = conn.execute(
                "INSERT OR IGNORE INTO raw_work_item(source_id,conversation_id,span_start,span_end,session_ref,status,created_at)"
                " VALUES(?,?,?,?,?,'new',?)", (source_id, conv_id, lo, hi, f"{sp}#{lo}-{hi}", now))
            created += cur.rowcount
        conn.execute("INSERT INTO cursor(source_id,workspace,stream_position) VALUES(?,?,?)"
                     " ON CONFLICT(source_id,workspace) DO UPDATE SET stream_position=excluded.stream_position",
                     (source_id, workspace, new_max))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK"); raise
    return created

def source_id_for(agent):
    m = {"claude_code": "ubuntu-cc", "codex": "ubuntu-codex",
         "gemini": "ubuntu-gemini", "pi_agent": "ubuntu-pi"}
    if agent in m: return m[agent]
    if agent.startswith("openclaw/"): return "ubuntu-oc-" + agent.split("/", 1)[1]
    return "ubuntu-" + agent.replace("/", "-")

_SPAN_SQL = """
SELECT m.id AS mid, m.idx AS idx, m.role AS role, m.content AS content,
       c.source_path AS source_path
FROM messages m JOIN conversations c ON c.id = m.conversation_id
WHERE m.conversation_id = :cid AND m.id BETWEEN :lo AND :hi ORDER BY m.idx ASC
"""
def read_span_messages(canon_db, conv_id, lo, hi):
    with closing(sqlite3.connect(f"file:{canon_db}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute(_SPAN_SQL, {"cid": conv_id, "lo": lo, "hi": hi}).fetchall()]
    return rows

def discover_sources(canon_db, conn):
    """codex R0 P0-2 / R1 P0-1：扫 CASS distinct (agent, workspace) —— known→处理 / released→纳入 / unknown→按(agent,workspace)quarantine（spec §2.6 agent/workspace 来源维度）。"""
    from distill import filters
    with closing(sqlite3.connect(f"file:{canon_db}?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT DISTINCT a.slug, w.path FROM agents a JOIN conversations c ON c.agent_id=a.id"
            " JOIN messages m ON m.conversation_id=c.id LEFT JOIN workspaces w ON w.id=c.workspace_id").fetchall()
    now = datetime.now(timezone.utc).isoformat()
    to_process, newly, seen = [], [], set()
    def _add(ag, ws_filter):
        sid = source_id_for(ag)
        key = (sid, ws_filter)
        if key in seen: return
        seen.add(key); to_process.append((sid, ag, ws_filter))
        if ws_filter == "":   # known/全量 → 清历史 specific-ws cursor 残行，避免晋升后冗余再读（R3 P2-1）
            conn.execute("DELETE FROM cursor WHERE source_id=? AND workspace!=''", (sid,))
    for ag, ws in rows:
        wsk = ws or ""
        cls = filters.classify_source(ag, ws)
        if cls == "distill":
            _add(ag, "")                                              # known agent → 全 workspace（ws 过滤=''）
        elif cls == "quarantine_unknown":
            row = conn.execute("SELECT status FROM source_quarantine WHERE agent=? AND workspace=?", (ag, wsk)).fetchone()
            if row and row[0] == "released":
                _add(ag, wsk)                                         # 仅放行该 (agent,workspace) 组合（R2 P0-2，不溢出别的 ws）
            else:
                cur = conn.execute("INSERT OR IGNORE INTO source_quarantine(agent,workspace,status,created_at)"
                                   " VALUES(?,?, 'quarantined', ?)", (ag, wsk, now))
                if cur.rowcount: newly.append(f"{ag}@{wsk}")
        # skip_self → 忽略（防自噬）
    conn.commit()
    return {"to_process": to_process, "newly_quarantined": newly}
