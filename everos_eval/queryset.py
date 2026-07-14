"""A 组查询集:07-13 后复杂新会话优先,不足放宽到未入 M1b 快照的复杂会话。"""
from __future__ import annotations
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from everos_probe.sampling import (count_tool_rounds, fetch_rows, has_pairable_extra,
                                   normalize_source, stable_hash)
from everos_adapter.cass_reader import read_message


@dataclass(frozen=True)
class Candidate:
    external_id: str
    conversation_id: int
    source: str
    n_rounds: int
    first_ts_ms: int  # CASS created_at 为毫秒 epoch,数值比较(codex R1:字符串比较静默错判)


def select_candidates(cands: list[Candidate], snapshot_eids: set, cutoff_ms: int, target: int = 30):
    pool = [c for c in cands if c.external_id not in snapshot_eids]
    post = [c for c in pool if c.first_ts_ms > cutoff_ms]
    if len(post) >= target:
        chosen, tier = post, "post_cutoff"
    else:
        chosen, tier = pool, "widened_non_snapshot"
    chosen = sorted(chosen, key=lambda c: stable_hash(c.external_id))[:target]
    return chosen, tier


def scan_complex_candidates(db_path: Path) -> list[Candidate]:
    """扫 CASS canonical(mode=ro),留 6+ tool-round、来源白名单、extra 可配对的会话。

    SQL/join 口径照搬 everos_probe/sampling.py:_CONV_SQL(agents 表 join、GROUP BY c.id
    HAVING COUNT(m.id) > 0),不自造。
    """
    uri = f"file:{db_path}?mode=ro"
    out: list[Candidate] = []
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row  # fetch_rows 内部 dict(r),必须 Row(照 M1b 口径)
        rows = conn.execute(
            """
            SELECT c.id AS id, a.slug AS agent, c.external_id AS external_id,
                   MIN(m.created_at) AS first_ts
            FROM conversations c
            JOIN agents a ON a.id = c.agent_id
            JOIN messages m ON m.conversation_id = c.id
            GROUP BY c.id
            HAVING COUNT(m.id) > 0
            """
        ).fetchall()
        for row in rows:
            conv_id = row["id"]
            eid = row["external_id"]
            slug = row["agent"]
            first_ts = row["first_ts"]
            src = normalize_source(slug)
            if src is None or not eid:
                continue
            mrows = fetch_rows(conn, conv_id)
            if not has_pairable_extra(mrows):
                continue
            n = count_tool_rounds(mrows)
            if n < 6:
                continue
            ts = int(first_ts)
            if ts < 10**12:  # 秒级则归一为毫秒;真库冒烟时实证单位后可收紧为断言
                ts *= 1000
            out.append(Candidate(eid, conv_id, src, n, ts))
    return out


def first_user_messages(mrows: list, k: int = 2) -> list[str]:
    """6-role 归一(经 cass_reader.read_message)后取前 k 条 user content。"""
    msgs: list[str] = []
    for r in mrows:
        m = read_message(r, ["extra_bin", "extra_json"])
        if m and m.get("role") == "user" and m.get("content"):
            msgs.append(m["content"])
            if len(msgs) >= k:
                break
    return msgs


def raw_baseline(user_msgs: list[str], cap: int = 500) -> str:
    return "\n".join(user_msgs)[:cap]


def load_snapshot_eids(snapshot_path: Path) -> set[str]:
    snap = json.loads(snapshot_path.read_text(encoding="utf-8"))
    return {s["external_id"] for lst in snap["strata"].values() for s in lst}
