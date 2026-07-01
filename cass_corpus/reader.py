# cass_corpus/reader.py
# 自包含只读 CASS canonical DB(不依赖/不修改 distill/cass_reader.py)。
# 只读全量会话消息 + 元数据,供 transcript 渲染。
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

_MSGS_SQL = "SELECT idx, role, content FROM messages WHERE conversation_id = ? ORDER BY idx ASC"


def _connect(db_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def select_conversations(db_path, limit=20, agents=None, min_turns=4, max_turns=None, since_ts=None):
    """挑会话元数据。agents=None → 全来源(不按 agent 筛);min_turns 轻量 floor;
    max_turns=None → 不设上限(长会话由 gbrain chunk 处理)。
    since_ts=None → 旧行为(最新 N, DESC);since_ts 给值 → 增量
    (last_message_created_at >= since_ts, ASC, 受 limit 安全帽)。返回 list[dict]。"""
    where_parts, params = [], []
    if agents:
        where_parts.append("a.slug IN (%s)" % ",".join("?" * len(agents)))
        params += list(agents)
    if since_ts is not None:
        where_parts.append("c.last_message_created_at >= ?")
        params.append(int(since_ts))
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(min_turns)
    max_clause = ""
    if max_turns is not None:
        max_clause = " AND COUNT(m.id) <= ?"
        params.append(max_turns)
    order = "ASC" if since_ts is not None else "DESC"
    params.append(limit)
    sql = _SELECT_SQL.format(where=where, max_clause=max_clause, order=order)
    with closing(_connect(db_path)) as db:
        rows = db.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def max_conversation_ts(db_path):
    """全库最大 last_message_created_at(首跑播种水位线用)。空库 → None。"""
    with closing(_connect(db_path)) as db:
        row = db.execute("SELECT MAX(last_message_created_at) AS m FROM conversations").fetchone()
    return row["m"] if row and row["m"] is not None else None


def read_messages(db_path, conv_id):
    """读一个会话的全部消息(按 idx 序)→ list[Msg]。"""
    with closing(_connect(db_path)) as db:
        rows = db.execute(_MSGS_SQL, (conv_id,)).fetchall()
    return [Msg(idx=r["idx"], role=r["role"], content=r["content"] or "") for r in rows]
