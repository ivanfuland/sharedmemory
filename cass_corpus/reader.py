# cass_corpus/reader.py
# 自包含只读 CASS canonical DB(不依赖/不修改 distill/cass_reader.py)。
# 只读全量会话消息 + 元数据,供 transcript 渲染。
import json
import sqlite3
from contextlib import closing

import msgpack

from cass_corpus.pruner import Msg

# 注:CASS 的 user_message_count/assistant_message_count 列普遍未填(NULL),
# 故用 COUNT(m.id) 真实消息数做轻量"实质性"floor;日期用 started_at,空则回退 last_message_created_at。
# 不按 agent 筛(归一化=统一格式;"值不值得记"交 gbrain 显著性闸逐会话判,agent 仅作 provenance 元数据)。
_SELECT_SQL = """
SELECT c.id AS id, a.slug AS agent, c.title AS title, w.path AS workspace,
       c.source_path AS source_path,{idcols}
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

# extra 的存储契约(CASS `src/storage/sqlite.rs::franken_message_insert_payload`):
#   非空 extra   → rmp_serde msgpack 进 `extra_bin`,`extra_json` = NULL   ← 真实数据的绝大多数
#   空对象 `{}`  → 字面 "{}" 进 `extra_json`,`extra_bin` = NULL
#   历史 raw 包装 → 原始 JSON 文本进 `extra_json`,`extra_bin` = NULL
# 二者互斥。只读 extra_json 会漏掉全部真实配对信息(实测真库 129734 条 tool 类消息里
# `extra_json LIKE '%tool_call_id%'` 命中 0 条,而解 extra_bin 拿到 126680 条 = 97.6%)。
_EXTRA_COLS = ("extra_bin", "extra_json")

# 稳定会话身份的来源列。老/合成 schema(既有 export_conv / incremental fixture)没有这两列
# → 探测后不选,render 走 legacy 回退(codex plan R0 P0:不得因缺列崩)。
_ID_COLS = ("external_id", "source_id")


def _idcols_sql(db):
    have = {r["name"] for r in db.execute("PRAGMA table_info(conversations)")}
    cols = [c for c in _ID_COLS if c in have]
    return "".join(f"\n       c.{c} AS {c}," for c in cols)


def _msgs_sql(extra_cols):
    """按库里真实存在的 extra 列拼 SELECT(老/合成 schema 可能一列都没有)。"""
    sel = "idx, role, content" + "".join(f", {c}" for c in extra_cols)
    return f"SELECT {sel} FROM messages WHERE conversation_id = ? ORDER BY idx ASC"


# 坏数据降级只针对"解码/格式"类异常;MemoryError / RecursionError 这类系统性资源错误
# 必须 loud fail,不能被降级路径吞掉(codex 复审 P2-#4)。
_DECODE_ERRORS = (msgpack.exceptions.UnpackException, msgpack.exceptions.ExtraData,
                  ValueError, TypeError, UnicodeDecodeError)


def _extra_dict(row, extra_cols):
    """取一条消息的 extra dict。extra_bin(msgpack) 优先,回退 extra_json。
    坏 msgpack / 坏 JSON / 非 dict → None(降级为无配对信息,不崩)。"""
    if "extra_bin" in extra_cols and row["extra_bin"] is not None:
        try:
            d = msgpack.unpackb(row["extra_bin"], raw=False, strict_map_key=False)
        except _DECODE_ERRORS:
            d = None
        if isinstance(d, dict):
            return d
    if "extra_json" in extra_cols and row["extra_json"]:
        try:
            d = json.loads(row["extra_json"])
        except (ValueError, TypeError):
            d = None
        if isinstance(d, dict):
            return d
    return None

# 按 id 精确取一条会话 meta（export_one 单条导出用）。列映射与 _SELECT_SQL 一致；
# JOIN messages + GROUP BY → 0 消息/不存在会话返回无行（None）。不设 min_turns floor（显著性交 min_chars）。
_GET_ONE_SQL = """
SELECT c.id AS id, a.slug AS agent, c.title AS title, w.path AS workspace,
       c.source_path AS source_path,{idcols}
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
    with closing(_connect(db_path)) as db:
        sql = _SELECT_SQL.format(where=where, max_clause=max_clause, order=order,
                                 idcols=_idcols_sql(db))
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


# 只对 6-role 的 tool_result 推导 unpaired。legacy tool/toolResult 全无配对信息,
# 逐条标记是噪声无信号;render 的 is_res fallback 已负责它们。
_RESULT_ROLE = "tool_result"


def read_messages(db_path, conv_id):
    """读一个会话的全部消息(按 idx 序)→ list[Msg]。
    有 `extra_bin`/`extra_json` 列时解析 tool_call_id(配对 id,给 render 用);
    一列都没有(老/合成 schema,如既有 incremental/export_conv fixture)→ 降级为无配对信息,不崩(codex plan R0 P0);
    坏 msgpack / 坏 JSON 同样降级。

    `unpaired` 由本函数**推导**,不从 extra 读:`extra.unpaired` 全链路无人写(franken/CASS
    源码均 0 命中),读它是死代码。reader 一次读完整个会话,手握全部 tool_call 的 id,
    判"这条结果配不上任何调用"比 franken 逐条 emit 时准。配对按 id 不按顺序(契约 P-原则-3)。"""
    with closing(_connect(db_path)) as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(messages)")}
        extra_cols = [c for c in _EXTRA_COLS if c in cols]
        rows = db.execute(_msgs_sql(extra_cols), (conv_id,)).fetchall()

    parsed = []
    for r in rows:
        d = _extra_dict(r, extra_cols) or {}
        # 类型收紧(codex 复审 P2)：Msg 契约是 Optional[str]。
        # `tool_call_id` 非字符串(bytes/int) → render 会渲染成 `[#b'abc']`，宁可当没有。
        tcid = d.get("tool_call_id")
        parsed.append((r, tcid if isinstance(tcid, str) and tcid else None))

    call_ids = {t for r, t in parsed if r["role"] == "tool_call" and t}
    return [Msg(idx=r["idx"], role=r["role"], content=r["content"] or "", tool_call_id=t,
                unpaired=(r["role"] == _RESULT_ROLE and (t is None or t not in call_ids)))
            for r, t in parsed]


def get_conversation(db_path, conv_id):
    """按 id 精确取一条会话 meta(export_one 单条导出用)。无消息/不存在 → None。"""
    with closing(_connect(db_path)) as db:
        row = db.execute(_GET_ONE_SQL.format(idcols=_idcols_sql(db)), (int(conv_id),)).fetchone()
    return dict(row) if row else None


def max_message_ts(db_path, conv_id):
    """该会话消息的 max(created_at)(= 文件真实内容版本;canonical messages.created_at 是毫秒 epoch)。
    read_messages 只返 Msg(idx,role,content) 无 created_at,故 export_one 靠这个取内容版本(codex R7 P1)。"""
    with closing(_connect(db_path)) as db:
        row = db.execute(
            "SELECT max(created_at) AS ts FROM messages WHERE conversation_id = ?", (int(conv_id),)
        ).fetchone()
    return row["ts"] if row and row["ts"] is not None else None
