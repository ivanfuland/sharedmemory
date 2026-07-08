# cass_corpus/reader.py
# 自包含只读 CASS canonical DB(不依赖/不修改 distill/cass_reader.py)。
# 只读全量会话消息 + 元数据,供 transcript 渲染。
import json
import sqlite3
from contextlib import closing
from cass_corpus.pruner import Msg

# 注:CASS 的 user_message_count/assistant_message_count 列普遍未填(NULL),
# 故用 COUNT(m.id) 真实消息数做轻量"实质性"floor;日期用 started_at,空则回退 last_message_created_at。
# 不按 agent 筛(归一化=统一格式;"值不值得记"交 gbrain 显著性闸逐会话判,agent 仅作 provenance 元数据)。
_SELECT_SQL = """
SELECT c.id AS id, a.slug AS agent, c.title AS title, w.path AS workspace,
       c.source_path AS source_path,
       COALESCE(c.started_at, c.last_message_created_at) AS started_at,
       c.last_message_created_at AS last_ts,
       COUNT(m.id) AS turns, c.primary_model AS model
FROM conversations c
JOIN agents a ON a.id = c.agent_id
LEFT JOIN workspaces w ON w.id = c.workspace_id
JOIN messages m ON m.conversation_id = c.id
{where}
GROUP BY c.id
HAVING COUNT(m.id) >= ?{max_clause}
ORDER BY c.last_message_created_at {order}
LIMIT ?
"""

_MSGS_SQL_BASE  = "SELECT idx, role, content FROM messages WHERE conversation_id = ? ORDER BY idx ASC"
_MSGS_SQL_EXTRA = "SELECT idx, role, content, extra_json FROM messages WHERE conversation_id = ? ORDER BY idx ASC"

# 按 id 精确取一条会话 meta（export_one 单条导出用）。列映射与 _SELECT_SQL 一致；
# JOIN messages + GROUP BY → 0 消息/不存在会话返回无行（None）。不设 min_turns floor（显著性交 min_chars）。
_GET_ONE_SQL = """
SELECT c.id AS id, a.slug AS agent, c.title AS title, w.path AS workspace,
       c.source_path AS source_path,
       COALESCE(c.started_at, c.last_message_created_at) AS started_at,
       c.last_message_created_at AS last_ts,
       COUNT(m.id) AS turns, c.primary_model AS model
FROM conversations c
JOIN agents a ON a.id = c.agent_id
LEFT JOIN workspaces w ON w.id = c.workspace_id
JOIN messages m ON m.conversation_id = c.id
WHERE c.id = ?
GROUP BY c.id
"""


def _connect(db_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def select_conversations(db_path, limit=20, agents=None, min_turns=4, max_turns=None, since_cursor=None):
    """挑会话元数据。agents=None → 全来源(不按 agent 筛);min_turns 轻量 floor;
    max_turns=None → 不设上限(长会话由 gbrain chunk 处理)。
    since_cursor=None → 旧行为(最新 N, DESC);since_cursor=(ts, id) → 严格 keyset 增量
    ((ts,id) 之后, ORDER BY ts ASC, id ASC, 受 limit 安全帽)。复合游标避免同 ts 会话被漏/wedge。
    返回 list[dict]。"""
    where_parts, params = [], []
    if agents:
        where_parts.append("a.slug IN (%s)" % ",".join("?" * len(agents)))
        params += list(agents)
    if since_cursor is not None:
        cts, cid = since_cursor
        where_parts.append("(c.last_message_created_at > ? OR (c.last_message_created_at = ? AND c.id > ?))")
        params += [int(cts), int(cts), int(cid)]
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(min_turns)
    max_clause = ""
    if max_turns is not None:
        max_clause = " AND COUNT(m.id) <= ?"
        params.append(max_turns)
    order = "ASC, c.id ASC" if since_cursor is not None else "DESC"
    params.append(limit)
    sql = _SELECT_SQL.format(where=where, max_clause=max_clause, order=order)
    with closing(_connect(db_path)) as db:
        rows = db.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def max_conversation_cursor(db_path):
    """全库最大复合游标 (last_message_created_at, id)(首跑播种用)。空库 → None。"""
    with closing(_connect(db_path)) as db:
        row = db.execute(
            "SELECT last_message_created_at AS ts, id FROM conversations "
            "ORDER BY last_message_created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return (row["ts"], row["id"]) if row and row["ts"] is not None else None


def read_messages(db_path, conv_id):
    """读一个会话的全部消息(按 idx 序)→ list[Msg]。
    **有 `extra_json` 列时**解析 tool_call_id/unpaired(配对标记,给 render 用);
    **无该列**(老/合成 schema,如既有 incremental/export_conv fixture)→ 降级为无配对信息,不崩(codex plan R0 P0);
    坏/空 JSON 同样降级。"""
    with closing(_connect(db_path)) as db:
        has_extra = any(r["name"] == "extra_json" for r in db.execute("PRAGMA table_info(messages)"))
        sql = _MSGS_SQL_EXTRA if has_extra else _MSGS_SQL_BASE
        rows = db.execute(sql, (conv_id,)).fetchall()
    msgs = []
    for r in rows:
        tcid, unpaired = None, False
        ej = r["extra_json"] if has_extra else None
        if ej:
            try:
                d = json.loads(ej)
                if isinstance(d, dict):
                    tcid = d.get("tool_call_id")
                    unpaired = bool(d.get("unpaired", False))
            except (ValueError, TypeError):
                pass                                        # 坏 JSON → 无配对信息,不崩
        msgs.append(Msg(idx=r["idx"], role=r["role"], content=r["content"] or "",
                        tool_call_id=tcid, unpaired=unpaired))
    return msgs


def get_conversation(db_path, conv_id):
    """按 id 精确取一条会话 meta(export_one 单条导出用)。无消息/不存在 → None。"""
    with closing(_connect(db_path)) as db:
        row = db.execute(_GET_ONE_SQL, (int(conv_id),)).fetchone()
    return dict(row) if row else None


def max_message_ts(db_path, conv_id):
    """该会话消息的 max(created_at)(= 文件真实内容版本;canonical messages.created_at 是毫秒 epoch)。
    read_messages 只返 Msg(idx,role,content) 无 created_at,故 export_one 靠这个取内容版本(codex R7 P1)。"""
    with closing(_connect(db_path)) as db:
        row = db.execute(
            "SELECT max(created_at) AS ts FROM messages WHERE conversation_id = ?", (int(conv_id),)
        ).fetchone()
    return row["ts"] if row and row["ts"] is not None else None
