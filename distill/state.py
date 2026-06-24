# distill/state.py
import sqlite3, fcntl
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    source_id TEXT NOT NULL,
    workspace TEXT NOT NULL DEFAULT '',
    stream_position INTEGER NOT NULL,
    PRIMARY KEY (source_id, workspace)
);
CREATE TABLE IF NOT EXISTS raw_work_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    conversation_id INTEGER NOT NULL,
    span_start INTEGER NOT NULL,
    span_end INTEGER NOT NULL,
    session_ref TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    deferred_days INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(source_id, conversation_id, span_start, span_end)
);
CREATE TABLE IF NOT EXISTS source_quarantine (
    agent TEXT NOT NULL,
    workspace TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'quarantined',   -- quarantined | released
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent, workspace)
);
CREATE TABLE IF NOT EXISTS journal (
    key TEXT PRIMARY KEY,
    raw_work_item_id INTEGER NOT NULL,
    entity_slug TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    fact_text TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    deferred_days INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS replay_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, layer TEXT NOT NULL, target TEXT NOT NULL,
    affected INTEGER NOT NULL, note TEXT
);
"""
RAW_BACKLOG = ("new", "raw_deferred")
JOURNAL_BACKLOG = ("pending", "deferred")

class ReplayError(Exception): pass

def _now(): return datetime.now(timezone.utc).isoformat()

def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

def _assert_one(conn, affected, layer, target):
    if affected != 1:
        conn.execute("INSERT INTO replay_ledger(ts,layer,target,affected,note) VALUES(?,?,?,?,?)",
                     (_now(), layer, target, affected, "affected!=1"))
        conn.commit()
        raise ReplayError(f"{layer} replay {target}: affected={affected} expected 1")

def replay_raw(conn, raw_id):
    cur = conn.execute("UPDATE raw_work_item SET status='new' WHERE id=? AND status='raw_quarantined'", (raw_id,))
    _assert_one(conn, cur.rowcount, "raw", str(raw_id)); conn.commit()

def replay_journal(conn, key):
    cur = conn.execute("UPDATE journal SET status='pending' WHERE key=? AND status='quarantined'", (key,))
    _assert_one(conn, cur.rowcount, "journal", key); conn.commit()

def total_backlog(conn):
    raw = conn.execute(f"SELECT COUNT(*) FROM raw_work_item WHERE status IN ({','.join('?'*len(RAW_BACKLOG))})", RAW_BACKLOG).fetchone()[0]
    jrn = conn.execute(f"SELECT COUNT(*) FROM journal WHERE status IN ({','.join('?'*len(JOURNAL_BACKLOG))})", JOURNAL_BACKLOG).fetchone()[0]
    return {"raw_backlog": raw, "journal_backlog": jrn, "total_backlog": raw + jrn}

def reset_deferred(conn, today):
    starved = []
    for tbl, term in (("raw_work_item", "raw_deferred"), ("journal", "deferred")):
        idcol = "id" if tbl == "raw_work_item" else "key"
        back = "new" if tbl == "raw_work_item" else "pending"
        for row in conn.execute(f"SELECT {idcol} AS k, deferred_days FROM {tbl} WHERE status=?", (term,)).fetchall():
            nd = row["deferred_days"] + 1
            conn.execute(f"UPDATE {tbl} SET status=?, deferred_days=? WHERE {idcol}=?", (back, nd, row["k"]))
            if nd >= 2: starved.append(row["k"])
    conn.commit()
    return {"raw_reset": True, "journal_reset": True, "starved": starved, "today": today}

@contextmanager
def flock_lease(lock_path):
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise SystemExit("distill-bridge: another instance holds lock; exiting")
    try:
        yield f
    finally:
        fcntl.flock(f, fcntl.LOCK_UN); f.close()
