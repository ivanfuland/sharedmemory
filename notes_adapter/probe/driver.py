"""Probe ledger + scanner + driver — spec §5.1/§5.2/§5.4/§5.5 M0 single-process
subset (see docs/projects/notes-memory/specs/2026-07-17-notes-knowledge-pipeline-design.md).

Public module contract: **zero hardcoded host paths / ports / env**. Every
host specific (db path, EverOS base URL, vault root, memory/store root,
python interpreter, repair runner, EverOS root for repair) is injected via
the `Driver` constructor or per-call keyword arguments by the private
harness. This file must stay safe to publish in the PUBLIC sharedmemory repo.

M0 scope note: the SQLite ledger below is a **single-process subset** of the
Postgres schema in spec §5.1 — same column names, but no `lease_id` /
`lease_expires_at` (M0 runs one synchronous driver, so the spec's
multi-worker lease fencing does not apply) and no Postgres-only features
(BIGSERIAL, JSONB, CHECK constraints expressed as SQL — those invariants are
enforced in Python here instead, by `Ledger.transition()`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from notes_adapter import identity, safe_open
from notes_adapter.coverage import char_multiset_recall
from notes_adapter.digest import TopicRow, verify_rows
from notes_adapter.frontmatter import PreparedDocument, prepare_document


class IllegalTransition(RuntimeError):
    """Raised when a status transition is not in the M0 allowed table, or the
    ledger row's current status doesn't match the caller's `from_status`
    (stale-read guard — the row moved under us)."""


class RepairPreconditionError(RuntimeError):
    """One of repair()'s mechanical preconditions was not satisfied, or the
    fence check right before the destructive step found the state had moved."""


# ---------------------------------------------------------------------------
# Scan primitives
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    """Producer contract for `Driver.scan_result()`. `ok=False` means the
    scan itself failed (broken mount, unreadable root sentinel, OSError
    during traversal) — the caller must NOT advance missing_streak on it.
    `refs` maps canonical_path -> SampleRef for every file the scan found
    (empty dict + ok=True is a legitimate "healthy, currently empty
    whitelist" result, distinct from ok=False)."""

    ok: bool
    refs: dict[str, identity.SampleRef] = field(default_factory=dict)


_MD_EXT = ".md"


def _walk_relative(root: str, sub: str):
    full = os.path.join(root, sub)
    if not os.path.isdir(full):
        return  # a whitelist entry that doesn't exist yet is not an error
    for entry in os.scandir(full):  # raises OSError on e.g. chmod 000 dir
        rel = f"{sub}/{entry.name}" if sub else entry.name
        if entry.is_dir(follow_symlinks=False):
            yield from _walk_relative(root, rel)
        elif entry.is_file(follow_symlinks=False) and rel.endswith(_MD_EXT):
            yield rel


class Scanner:
    """Canary (mount health) -> whitelist glob -> safe-open hash, one
    transaction's worth of work per call (spec §5.3).

    Canary judges *mount health only*: root directory exists + a root-level
    sentinel file is readable. The sentinel is independent of the whitelist
    directories — a whitelist dir being legitimately empty must NOT look
    like a broken mount (R7 P0-1).

    Any OSError during traversal (a chmod 000 subdirectory, a vanished
    directory mid-walk, ...) aborts the *whole* scan run with `ok=False` and
    an empty diff — the caller must not interpret "found zero files because
    the scan itself broke" as "every file got deleted".
    """

    def __init__(self, sentinel: str = ".m0-root-sentinel"):
        self.sentinel = sentinel

    def scan(self, vault_root: str, whitelist: list[str]) -> ScanResult:
        if not os.path.isdir(vault_root):
            return ScanResult(ok=False, refs={})
        try:
            with open(os.path.join(vault_root, self.sentinel), "rb"):
                pass
        except OSError:
            return ScanResult(ok=False, refs={})

        refs: dict[str, identity.SampleRef] = {}
        try:
            for wl in whitelist:
                for rel in _walk_relative(vault_root, wl):
                    data = safe_open.read_within(vault_root, rel)
                    cpath = identity.canonical_path(rel)
                    refs[cpath] = identity.SampleRef(
                        canonical_path=cpath,
                        staged_relpath=rel,
                        vault_raw_hash=identity.content_hash(data),
                        staged_sha256=hashlib.sha256(data).hexdigest(),
                    )
        except (OSError, safe_open.UnsafePathError):
            return ScanResult(ok=False, refs={})
        return ScanResult(ok=True, refs=refs)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at TEXT,
  ok INTEGER NOT NULL,
  files_seen INTEGER,
  whitelist_epoch INTEGER NOT NULL DEFAULT 0,
  note TEXT
);

CREATE TABLE IF NOT EXISTS notes_ledger (
  path TEXT PRIMARY KEY,
  path_hash TEXT UNIQUE NOT NULL,
  admitted INTEGER NOT NULL DEFAULT 1,
  active_doc_id TEXT,
  active_generation INTEGER,
  active_doc_dir TEXT,
  staging_doc_id TEXT,
  staging_doc_dir TEXT,
  desired_hash TEXT NOT NULL,
  synced_hash TEXT,
  desired_fingerprint TEXT NOT NULL DEFAULT '',
  synced_fingerprint TEXT,
  operation TEXT NOT NULL,
  status TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  generation_attempts INTEGER NOT NULL DEFAULT 0,
  last_seen_scan_id INTEGER,
  missing_streak INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  dead_reason TEXT,
  topic_count INTEGER,
  frontmatter TEXT,
  synced_frontmatter TEXT,
  first_seen TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_synced TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS notes_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  operation TEXT NOT NULL,
  generation INTEGER NOT NULL,
  desired_hash TEXT,
  request_id TEXT,
  spend_delta_usd REAL,
  estimated_llm_tokens INTEGER,
  tokenizer_version TEXT,
  outcome TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS cleanup_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id TEXT NOT NULL,
  md_path TEXT,
  src_path TEXT NOT NULL,
  src_generation INTEGER NOT NULL,
  reason TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  done INTEGER NOT NULL DEFAULT 0,
  dead INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS cleanup_pending_doc
  ON cleanup_jobs(doc_id) WHERE NOT done AND NOT dead;
"""

# M0 single-process status transition table (spec §5.4 subset; no lease
# fencing — see module docstring). Anything not listed here is illegal,
# including e.g. synced -> indexing directly (must go through
# synced -> pending -> in_flight -> indexing to pick up a new generation).
_ALLOWED_TRANSITIONS = {
    ("pending", "in_flight"),
    ("in_flight", "indexing"),
    ("in_flight", "failed"),
    ("failed", "pending"),
    ("indexing", "synced"),
    ("indexing", "failed"),
    ("synced", "pending"),  # new generation (update) picked up by a scan
    ("synced", "retiring"),  # truth-delete two-scan-confirm, or coverage demotion
    ("retiring", "deleted"),
    ("retiring", "failed"),  # demote_for_coverage: reason=coverage
}


class Ledger:
    """SQLite-backed ledger — spec §5.1 single-process subset. All status
    mutation goes through `transition()` (existing rows) or `insert_pending`/
    `revive` (new rows / tombstone revival); `IllegalTransition` is the only
    way an invalid state change can be attempted from outside this class."""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    # -- reads ---------------------------------------------------------
    def get(self, path: str):
        cur = self._conn.execute("SELECT * FROM notes_ledger WHERE path=?", (path,))
        return cur.fetchone()

    def all_rows(self) -> dict[str, sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM notes_ledger")
        return {r["path"]: r for r in cur.fetchall()}

    def is_cleanup_queued(self, doc_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM cleanup_jobs WHERE doc_id=? AND NOT done", (doc_id,)
        )
        return cur.fetchone() is not None

    def pending_cleanup_jobs(self):
        cur = self._conn.execute(
            "SELECT * FROM cleanup_jobs WHERE NOT done AND NOT dead ORDER BY id"
        )
        return cur.fetchall()

    # -- row lifecycle (outside the transition table: brand-new / tombstone) --
    def insert_pending(
        self, path: str, ref: identity.SampleRef, generation: int, operation: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO notes_ledger "
            "(path, path_hash, desired_hash, desired_fingerprint, operation, "
            " status, generation, missing_streak) VALUES (?,?,?,?,?,?,?,0)",
            (
                path,
                identity.path_hash(path),
                ref.vault_raw_hash,
                "",
                operation,
                "pending",
                generation,
            ),
        )
        self._conn.commit()

    def revive(self, path: str, ref: identity.SampleRef, generation: int) -> None:
        """Tombstone revival (spec §5.3): a deleted path reappears ->
        unconditional create at a new generation, new token."""
        cur = self._conn.execute(
            "UPDATE notes_ledger SET status='pending', operation='create', "
            "generation=?, desired_hash=?, desired_fingerprint='', missing_streak=0, "
            "active_doc_id=NULL, active_doc_dir=NULL, active_generation=NULL, "
            "staging_doc_id=NULL, staging_doc_dir=NULL, admitted=1, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE path=? AND status='deleted'",
            (generation, ref.vault_raw_hash, path),
        )
        if cur.rowcount != 1:
            raise IllegalTransition(f"{path}: revive() requires an existing status=deleted row")
        self._conn.commit()

    def touch_desired_hash(self, path: str, vault_raw_hash: str) -> None:
        """In-place desired_hash update for a row that is mid-flight
        (pending/in_flight/indexing/failed) — no generation bump, no status
        change. The in-progress attempt reads-fresh from disk anyway, so this
        is bookkeeping only: it keeps the next scan's diff honest."""
        self._conn.execute(
            "UPDATE notes_ledger SET desired_hash=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE path=?",
            (vault_raw_hash, path),
        )
        self._conn.commit()

    def bump_missing_streak(self, path: str, streak: int) -> None:
        self._conn.execute(
            "UPDATE notes_ledger SET missing_streak=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE path=?",
            (streak, path),
        )
        self._conn.commit()

    # -- the state machine ----------------------------------------------
    def transition(self, path: str, from_status: str, to_status: str, **fields) -> None:
        if (from_status, to_status) not in _ALLOWED_TRANSITIONS:
            raise IllegalTransition(
                f"{from_status!r} -> {to_status!r} is not an allowed M0 transition"
            )
        set_cols = ["status = ?"]
        values: list = [to_status]
        for k, v in fields.items():
            set_cols.append(f"{k} = ?")
            values.append(v)
        set_cols.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        sql = f"UPDATE notes_ledger SET {', '.join(set_cols)} WHERE path = ? AND status = ?"
        values.extend([path, from_status])
        cur = self._conn.execute(sql, values)
        if cur.rowcount != 1:
            row = self.get(path)
            actual = row["status"] if row else None
            raise IllegalTransition(
                f"{path}: expected status={from_status!r} to transition -> {to_status!r}, "
                f"found {actual!r} (stale read or truly illegal)"
            )
        self._conn.commit()

    # -- adoption (StoreReader-snapshot recovery) -----------------------
    def adopt_active(self, path: str, doc_id: str, doc_dir: str) -> bool:
        cur = self._conn.execute(
            "UPDATE notes_ledger SET active_doc_id=?, active_doc_dir=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE path=? AND active_doc_id IS NULL",
            (doc_id, doc_dir, path),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def adopt_staging(self, path: str, doc_id: str, doc_dir: str) -> bool:
        cur = self._conn.execute(
            "UPDATE notes_ledger SET staging_doc_id=?, staging_doc_dir=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE path=? AND staging_doc_id IS NULL",
            (doc_id, doc_dir, path),
        )
        self._conn.commit()
        return cur.rowcount == 1

    # -- audit / cleanup --------------------------------------------------
    def record_scan_run(self, ok: bool, files_seen: int, whitelist_epoch: int = 0, note=None) -> int:
        cur = self._conn.execute(
            "INSERT INTO scan_runs (completed_at, ok, files_seen, whitelist_epoch, note) "
            "VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?, ?, ?)",
            (int(ok), files_seen, whitelist_epoch, note),
        )
        self._conn.commit()
        return cur.lastrowid

    def record_attempt(
        self,
        path: str,
        operation: str,
        generation: int,
        outcome: str,
        *,
        desired_hash=None,
        request_id=None,
        spend_delta_usd=None,
        estimated_llm_tokens=None,
        tokenizer_version=None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO notes_attempts (path, operation, generation, desired_hash, "
            "request_id, spend_delta_usd, estimated_llm_tokens, tokenizer_version, outcome) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                path,
                operation,
                generation,
                desired_hash,
                request_id,
                spend_delta_usd,
                estimated_llm_tokens,
                tokenizer_version,
                outcome,
            ),
        )
        self._conn.commit()

    def enqueue_cleanup(
        self, doc_id: str, src_path: str, src_generation: int, reason: str, md_path=None
    ) -> None:
        self._conn.execute(
            "INSERT INTO cleanup_jobs (doc_id, md_path, src_path, src_generation, reason) "
            "VALUES (?,?,?,?,?)",
            (doc_id, md_path, src_path, src_generation, reason),
        )
        self._conn.commit()

    def mark_cleanup_done(self, job_id: int) -> None:
        self._conn.execute(
            "UPDATE cleanup_jobs SET done=1, "
            "finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (job_id,),
        )
        self._conn.commit()

    def mark_cleanup_attempt(self, job_id: int, error=None, dead: bool = False) -> None:
        self._conn.execute(
            "UPDATE cleanup_jobs SET attempts=attempts+1, last_error=?, dead=? WHERE id=?",
            (error, int(dead), job_id),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# FakeRemote — in-memory test double (Step 1 state-machine tests)
# ---------------------------------------------------------------------------


class FakeRemote:
    """In-memory Remote test double implementing POST/DELETE/list.
    Immediately consistent: a posted doc is fully "there" the instant
    `post()` returns, so the state-machine tests don't need to model
    eventual-consistency polling — that's HttpRemote + StoreReader's job
    against a real instance (Task 11), out of scope here."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.cleanup_requested: set[str] = set()
        self._seq = 0

    def post(self, doc: PreparedDocument):
        self._seq += 1
        doc_id = f"fake-doc-{self._seq}"
        doc_dir = f"/fake/{doc_id}"
        self.docs[doc_id] = {
            "source_token": doc.source_token,
            "title": doc.title,
            "category_id": doc.category_id,
            "doc_dir": doc_dir,
            "topic_count": 1,
        }
        return doc_id, doc_dir, 1

    def delete(self, doc_id: str) -> int:
        existed = doc_id in self.docs
        self.docs.pop(doc_id, None)
        self.cleanup_requested.discard(doc_id)
        return 1 if existed else 0

    def request_cleanup(self, doc_id: str) -> None:
        self.cleanup_requested.add(doc_id)

    def list_docs(self, page: int = 1):
        return list(self.docs.keys())

    def exists(self, doc_id: str) -> bool:
        return doc_id in self.docs


# ---------------------------------------------------------------------------
# HttpRemote — thin contract client for the real EverOS instance (:8031)
# ---------------------------------------------------------------------------


def _unwrap_envelope(payload) -> dict:
    if not isinstance(payload, dict) or "data" not in payload:
        raise ValueError(f"malformed EverOS envelope (expected request_id/data): {payload!r}")
    data = payload["data"]
    if not isinstance(data, dict):
        raise ValueError(f"malformed EverOS envelope: data is not an object: {payload!r}")
    return data


def _build_multipart(boundary: str, doc: PreparedDocument) -> bytes:
    parts: list[bytes] = []

    def _field(name: str, value: str) -> None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode(
                "utf-8"
            )
        )

    _field("title", doc.title)
    _field("category_id", doc.category_id)
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{doc.source_token}"\r\nContent-Type: text/markdown\r\n\r\n'.encode("utf-8")
        + doc.redacted
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts)


class HttpRemote:
    """Thin HTTP client for a real EverOS instance. Contract-tested against
    hand-written synthetic same-shape fixtures (Task 8) — never exercised
    against a live instance in this task's test suite (that's Task 11)."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, data: bytes | None = None, headers=None) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read()
        return json.loads(body.decode("utf-8"))

    def post(self, doc: PreparedDocument):
        boundary = "m0probe" + hashlib.sha1(doc.source_token.encode()).hexdigest()[:16]
        body = _build_multipart(boundary, doc)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        payload = self._request(
            "POST", "/api/v1/knowledge/documents", data=body, headers=headers
        )
        data = _unwrap_envelope(payload)
        doc_id, doc_dir = data.get("doc_id"), data.get("doc_dir")
        if not doc_id or not doc_dir:
            raise ValueError(f"HttpRemote.post: response missing doc_id/doc_dir: {payload!r}")
        return doc_id, doc_dir, data.get("topic_count")

    def delete(self, doc_id: str) -> int:
        payload = self._request("DELETE", f"/api/v1/knowledge/documents/{doc_id}")
        data = _unwrap_envelope(payload)
        return int(data.get("deleted", 0))

    def list_docs(self, page: int = 1):
        payload = self._request("GET", f"/api/v1/knowledge/documents?page={page}")
        data = _unwrap_envelope(payload)
        items = data.get("items")
        if not isinstance(items, list):
            raise ValueError(f"HttpRemote.list_docs: response missing items array: {payload!r}")
        return items

    def search(self, query: str, **kwargs):
        payload = self._request(
            "GET", f"/api/v1/knowledge/search?q={urllib.parse.quote(query)}"
        )
        data = _unwrap_envelope(payload)
        hits = data.get("hits")
        if not isinstance(hits, list):
            raise ValueError(f"HttpRemote.search: response missing hits array: {payload!r}")
        for h in hits:
            doc = h.get("document") if isinstance(h, dict) else None
            if not isinstance(doc, dict) or not doc.get("doc_id"):
                raise ValueError(f"HttpRemote.search: hit missing document.doc_id: {h!r}")
        return hits


# ---------------------------------------------------------------------------
# StoreReader — direct three-store reader (SQLite / Lance / topic markdown)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"^n([0-9a-f]+)-g(\d+)\.md$")


def parse_source_token(token: str):
    m = _TOKEN_RE.match(token)
    if not m:
        return None
    return m.group(1), int(m.group(2))


class StoreReader:
    """Direct reader over the three on-disk EverOS stores. M0 note: unit
    tested here against small synthetic on-disk fixtures (a temp SQLite with
    the expected columns, a temp markdown dir, a JSON sidecar standing in
    for the real Lance table) — real-store shape validation is Task 11's
    job against a live instance.

    Expected layout under `root`:
      root/system.db                    - knowledge_documents, knowledge_topics
      root/lance/<doc_id>.json          - {node_id: canonical_digest} sidecar
      root/documents/<doc_id>/index.md  - frontmatter incl. `source_token:`
      root/documents/<doc_id>/<node_id>.md
      root/md_change_state.db           - cascade queue (processed flag)
    """

    def __init__(self, root: str):
        self.root = root

    def _sqlite_path(self) -> str:
        return os.path.join(self.root, "system.db")

    def sqlite_topics(self, doc_id: str) -> list[TopicRow]:
        path = self._sqlite_path()
        if not os.path.exists(path):
            return []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT node_id, summary, topic_name, topic_path, category_id, depth, "
                "content, content_sha256 FROM knowledge_topics WHERE doc_id=?",
                (doc_id,),
            )
            return [TopicRow(**dict(r)) for r in cur.fetchall()]
        finally:
            conn.close()

    def document_row(self, doc_id: str):
        path = self._sqlite_path()
        if not os.path.exists(path):
            return None
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT doc_id, source_token, doc_dir FROM knowledge_documents WHERE doc_id=?",
                (doc_id,),
            )
            return cur.fetchone()
        finally:
            conn.close()

    def document_row_by_token(self, source_token: str):
        path = self._sqlite_path()
        if not os.path.exists(path):
            return None
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT doc_id, source_token, doc_dir FROM knowledge_documents WHERE source_token=?",
                (source_token,),
            )
            return cur.fetchone()
        finally:
            conn.close()

    def lance_digests(self, doc_id: str) -> dict:
        path = os.path.join(self.root, "lance", f"{doc_id}.json")
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def topic_markdowns(self, doc_dir: str) -> dict:
        full = doc_dir if os.path.isabs(doc_dir) and os.path.isdir(doc_dir) else os.path.join(
            self.root, doc_dir.lstrip("/")
        )
        out: dict[str, str] = {}
        if not os.path.isdir(full):
            return out
        for name in os.listdir(full):
            if not name.endswith(".md") or name == "index.md":
                continue
            node_id = name[: -len(".md")]
            with open(os.path.join(full, name), "r", encoding="utf-8") as f:
                out[node_id] = f.read()
        return out

    def iter_document_dirs(self):
        """Markdown-first enumeration (R9 nit5): walk doc dirs on disk rather
        than the SQLite documents table, so a POST that landed markdown but
        crashed before the document-row commit is still discoverable —
        covers the "document row missing, markdown present" adoption window."""
        base = os.path.join(self.root, "documents")
        if not os.path.isdir(base):
            return
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            if not os.path.isdir(full):
                continue
            index_path = os.path.join(full, "index.md")
            if not os.path.exists(index_path):
                continue
            with open(index_path, "r", encoding="utf-8") as f:
                head = f.read(4096)
            m = re.search(r"^source_token:\s*(\S+)\s*$", head, re.MULTILINE)
            if not m:
                continue
            yield m.group(1), full

    def cascade_pending(self) -> int:
        path = os.path.join(self.root, "md_change_state.db")
        if not os.path.exists(path):
            return 0
        conn = sqlite3.connect(path)
        try:
            cur = conn.execute("SELECT COUNT(*) FROM md_change_state WHERE processed=0")
            return cur.fetchone()[0]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

_DEFAULT_TAXONOMY = frozenset({"Others"})
_DEFAULT_CFG = {"schema": "m0-probe", "adapter_version": "0"}


class Driver:
    """State-machine driver (spec §5.1/§5.4 M0 single-process subset).

    Every host specific is a constructor arg or a per-call kwarg — this
    class hardcodes no path/port/env of its own (public-repo module).
    """

    def __init__(
        self,
        db_path: str,
        base_url_or_remote,
        vault_root: str,
        memory_root: str | None = None,
        python_exe: str | None = None,
        repair_runner=None,
        sentinel: str = ".m0-root-sentinel",
        taxonomy=_DEFAULT_TAXONOMY,
        cfg=_DEFAULT_CFG,
    ):
        self.ledger = Ledger(db_path)
        self.remote = (
            base_url_or_remote
            if hasattr(base_url_or_remote, "post")
            else HttpRemote(base_url_or_remote)
        )
        self.vault_root = vault_root
        self.memory_root = memory_root
        self.python_exe = python_exe
        self.repair_runner = repair_runner
        self.store_reader = StoreReader(memory_root) if memory_root else None
        self.scanner = Scanner(sentinel=sentinel)
        self.taxonomy = taxonomy
        self.cfg = cfg
        self._refs: dict[str, identity.SampleRef] = {}

    def row(self, path: str):
        return self.ledger.get(path)

    # -- scanning ---------------------------------------------------------
    def scan(self, paths: list[str]) -> dict:
        """Targeted rescan of explicit relpaths (no whitelist glob/canary —
        for that, use `scan_whitelist`). Used for "I already know which
        files changed" rescans."""
        refs = {}
        for rel in paths:
            data = safe_open.read_within(self.vault_root, rel)
            cpath = identity.canonical_path(rel)
            refs[cpath] = identity.SampleRef(
                canonical_path=cpath,
                staged_relpath=rel,
                vault_raw_hash=identity.content_hash(data),
                staged_sha256=hashlib.sha256(data).hexdigest(),
            )
        return self.scan_result(ScanResult(ok=True, refs=refs))

    def scan_whitelist(self, whitelist: list[str]) -> dict:
        return self.scan_result(self.scanner.scan(self.vault_root, whitelist))

    def scan_result(self, sr: ScanResult) -> dict:
        """Sole diff entry point (spec §5.3). `ok=False` -> zero diff, does
        NOT advance missing_streak on any row."""
        diff = {"ok": sr.ok, "created": [], "updated": [], "missing": []}
        if not sr.ok:
            self.ledger.record_scan_run(ok=False, files_seen=0)
            return diff
        self.ledger.record_scan_run(ok=True, files_seen=len(sr.refs))

        existing = self.ledger.all_rows()
        seen = set(sr.refs)
        for path, ref in sr.refs.items():
            self._refs[path] = ref
            row = existing.get(path)
            if row is None:
                self.ledger.insert_pending(path, ref, generation=0, operation="create")
                diff["created"].append(path)
            elif row["status"] == "deleted":
                self.ledger.revive(path, ref, generation=row["generation"] + 1)
                diff["created"].append(path)
            elif row["desired_hash"] != ref.vault_raw_hash:
                if row["status"] == "synced":
                    self.ledger.transition(
                        path,
                        "synced",
                        "pending",
                        desired_hash=ref.vault_raw_hash,
                        desired_fingerprint="",
                        generation=row["generation"] + 1,
                        operation="update",
                        missing_streak=0,
                    )
                    diff["updated"].append(path)
                else:
                    # mid-flight (pending/in_flight/indexing/failed): update
                    # desired_hash in place — no generation bump, the current
                    # attempt reads-fresh from disk when it gets there.
                    self.ledger.touch_desired_hash(path, ref.vault_raw_hash)
            else:
                if row["missing_streak"] != 0:
                    self.ledger.bump_missing_streak(path, 0)

        for path, row in existing.items():
            if path in seen or row["status"] == "deleted":
                continue
            streak = row["missing_streak"] + 1
            if streak >= 2 and row["status"] == "synced":
                self.ledger.transition(
                    path, "synced", "retiring", missing_streak=streak, operation="delete"
                )
                diff["missing"].append(path)
            else:
                self.ledger.bump_missing_streak(path, streak)
        return diff

    # -- worker turn (spec §5.4) -------------------------------------------
    def _claim_next(self):
        rows = self.ledger.all_rows()
        for statuses in (("retiring",), ("indexing",), ("pending", "failed")):
            for row in rows.values():
                if row["status"] in statuses:
                    return row
        return None

    def run_once(self):
        """Claim exactly one ledger row and advance it one worker turn:
        pending/failed -> POST -> indexing; indexing -> accept -> synced (or
        failed); retiring -> delete -> deleted. Returns None if there was
        nothing to do."""
        row = self._claim_next()
        if row is None:
            return None
        status = row["status"]
        if status in ("pending", "failed"):
            return self._claim_and_post(row)
        if status == "indexing":
            return self._accept_and_promote(row)
        if status == "retiring":
            return self._complete_delete(row)
        return None

    def _claim_and_post(self, row):
        path = row["path"]
        self.ledger.transition(
            path, row["status"], "in_flight", generation_attempts=row["generation_attempts"] + 1
        )
        ref = self._refs.get(path)
        if ref is None:
            data = safe_open.read_within(self.vault_root, path)
            ref = identity.SampleRef(
                path, path, identity.content_hash(data), hashlib.sha256(data).hexdigest()
            )
            self._refs[path] = ref

        # read-source self-check (spec §5.4 step 2): reread fresh, abandon
        # this turn if it drifted since the scan that queued it.
        staged_bytes = safe_open.read_within(self.vault_root, ref.staged_relpath)
        if hashlib.sha256(staged_bytes).hexdigest() != ref.staged_sha256:
            self.ledger.transition(
                path, "in_flight", "failed", last_error="source drifted before POST"
            )
            self.ledger.record_attempt(
                path, row["operation"], row["generation"], outcome="drift-abort"
            )
            return {"path": path, "status": "failed"}

        doc = prepare_document(ref, staged_bytes, row["generation"], self.taxonomy, self.cfg)
        doc_id, doc_dir, topic_count = self.remote.post(doc)
        self.ledger.transition(
            path,
            "in_flight",
            "indexing",
            staging_doc_id=doc_id,
            staging_doc_dir=doc_dir,
            topic_count=topic_count,
            desired_fingerprint=doc.fingerprint,
        )
        self.ledger.record_attempt(
            path,
            row["operation"],
            row["generation"],
            outcome="posted",
            desired_hash=row["desired_hash"],
        )
        return {"path": path, "status": "indexing", "staging_doc_id": doc_id}

    def _accept_and_promote(self, row):
        path = row["path"]
        ok, reason = self.accept(path)
        if not ok:
            if row["staging_doc_id"]:
                self.remote.delete(row["staging_doc_id"])
            self.ledger.transition(
                path,
                "indexing",
                "failed",
                last_error=reason or "accept failed",
                staging_doc_id=None,
                staging_doc_dir=None,
            )
            self.ledger.record_attempt(
                path, row["operation"], row["generation"], outcome="accept-failed"
            )
            return {"path": path, "status": "failed", "reason": reason}

        old_doc_id = row["active_doc_id"]
        old_generation = row["active_generation"]
        new_doc_id = row["staging_doc_id"]
        self.ledger.transition(
            path,
            "indexing",
            "synced",
            active_doc_id=new_doc_id,
            active_doc_dir=row["staging_doc_dir"],
            active_generation=row["generation"],
            staging_doc_id=None,
            staging_doc_dir=None,
            synced_hash=row["desired_hash"],
            synced_fingerprint=row["desired_fingerprint"],
            operation="none",
        )
        self.ledger.record_attempt(path, row["operation"], row["generation"], outcome="synced")

        if old_doc_id and old_doc_id != new_doc_id:
            self.ledger.enqueue_cleanup(
                old_doc_id,
                src_path=path,
                src_generation=old_generation or 0,
                reason="superseded-by-update",
            )
            if hasattr(self.remote, "request_cleanup"):
                self.remote.request_cleanup(old_doc_id)
        return {"path": path, "status": "synced"}

    def _complete_delete(self, row):
        path = row["path"]
        doc_id = row["active_doc_id"]
        if doc_id:
            self.remote.delete(doc_id)
        self.ledger.transition(
            path,
            "retiring",
            "deleted",
            active_doc_id=None,
            active_doc_dir=None,
            active_generation=None,
            admitted=0,
        )
        self.ledger.record_attempt(path, "delete", row["generation"], outcome="deleted")
        return {"path": path, "status": "deleted"}

    # -- accept: bounded poll over three stores + digest + coverage --------
    def accept(self, path: str, max_attempts: int = 5, interval: float = 0.0):
        row = self.ledger.get(path)
        if row is None or row["status"] != "indexing":
            raise IllegalTransition(f"{path}: accept() requires status=indexing")
        doc_id = row["staging_doc_id"]
        last_reason = "no attempts made"
        for attempt in range(max_attempts):
            ok, reason = self._verify_staging(row, doc_id)
            if ok:
                return True, None
            last_reason = reason
            if attempt < max_attempts - 1 and interval:
                time.sleep(interval)
        return False, last_reason

    def _verify_staging(self, row, doc_id: str):
        if self.store_reader is None:
            # No on-disk store configured (FakeRemote-only tests): trust the
            # remote's own bookkeeping — it's immediately consistent by
            # construction (FakeRemote) or the caller accepts that tradeoff.
            exists_fn = getattr(self.remote, "exists", None)
            exists = exists_fn(doc_id) if exists_fn else doc_id in getattr(self.remote, "docs", {})
            return (True, None) if exists else (False, "doc not visible on remote")

        doc_row = self.store_reader.document_row(doc_id)
        if doc_row is None:
            return False, "document row missing"
        sqlite_rows = self.store_reader.sqlite_topics(doc_id)
        if not sqlite_rows:
            return False, "no topic rows"
        lance = self.store_reader.lance_digests(doc_id)
        # staging_doc_dir is populated during accept() (status=indexing);
        # active_doc_dir is what's populated once synced — repair()
        # reverifies an already-synced doc, so it needs the latter.
        doc_dir = row["staging_doc_dir"] or row["active_doc_dir"] or ""
        md = self.store_reader.topic_markdowns(doc_dir)
        bad = verify_rows(sqlite_rows, lance, md)
        if bad:
            return False, f"verify_rows: {bad}"

        # Coverage: first round is measure-only (spec §5.4 step 5) — recorded
        # to notes_attempts, never gates accept() by itself in M0. Gating
        # only happens later via demote_for_coverage() in the reaccept phase.
        source_text = self._read_source_text(row["path"])
        if source_text is not None:
            recall = char_multiset_recall(source_text, [r.content for r in sqlite_rows])
            self.ledger.record_attempt(
                row["path"],
                row["operation"],
                row["generation"],
                outcome=f"coverage-measured:{recall:.4f}",
            )
        return True, None

    def _read_source_text(self, path: str):
        try:
            data = safe_open.read_within(self.vault_root, path)
        except (OSError, safe_open.UnsafePathError):
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    # -- reaccept / coverage demotion (spec §5.4 step 5, "实体阶段") --------
    def demote_for_coverage(self, path: str):
        """synced -> retiring -> [delete active doc] -> failed(reason=coverage).
        Used once the calibration phase has frozen a coverage threshold and a
        previously-synced doc no longer clears it on reaccept."""
        row = self.ledger.get(path)
        if row is None or row["status"] != "synced":
            raise IllegalTransition(f"{path}: demote_for_coverage requires status=synced")
        doc_id = row["active_doc_id"]
        self.ledger.transition(path, "synced", "retiring", operation="delete")
        if doc_id:
            self.remote.delete(doc_id)
        self.ledger.transition(
            path,
            "retiring",
            "failed",
            last_error="coverage below frozen threshold",
            active_doc_id=None,
            active_doc_dir=None,
            active_generation=None,
        )
        self.ledger.record_attempt(
            path, "delete", row["generation"], outcome="demoted-coverage"
        )
        return {"path": path, "status": "failed", "reason": "coverage"}

    # -- cleanup executor (spec §5.4 step 6 / R3 P0-1) ----------------------
    def run_cleanup(self):
        """Consume the cleanup queue: for each pending job, actually DELETE
        the old doc and confirm it's gone (not just mark the job done)."""
        results = []
        for job in self.ledger.pending_cleanup_jobs():
            doc_id = job["doc_id"]
            self.remote.delete(doc_id)
            exists_fn = getattr(self.remote, "exists", None)
            still_present = (
                exists_fn(doc_id) if exists_fn else doc_id in getattr(self.remote, "docs", {})
            )
            if not still_present and self.store_reader is not None:
                still_present = self.store_reader.document_row(doc_id) is not None
            if still_present:
                self.ledger.mark_cleanup_attempt(job["id"], error="doc still present after delete")
                results.append({"doc_id": doc_id, "done": False})
                continue
            self.ledger.mark_cleanup_done(job["id"])
            results.append({"doc_id": doc_id, "done": True})
        return results

    # -- adoption (spec §5.3 "收养快照" / §5.1 authority order) -------------
    def adopt_snapshot(self):
        """StoreReader full listing -> pathhash-prefix grouping -> §5.1
        authority order. Four scenarios:
          (1) doc generation == ledger active_generation, ledger active_doc_id
              missing -> adopt into active (crash recovery: remote write
              landed, ledger write didn't).
          (2) doc generation == ledger's current-intent generation while the
              row is in_flight/indexing with no staging pointer recorded yet
              -> adopt into staging only (promotion still requires the full
              accept path — adoption never shortcuts CAS).
          (3) same generation, multiple doc_ids for the same path -> isolate,
              do not pick a winner (F-N2 candidate).
          (4) doc_id already queued in cleanup_jobs -> refuse adoption
              outright (already-known garbage never gets resurrected).
        """
        result = {"adopted_active": [], "adopted_staging": [], "isolated": [], "refused": []}
        if self.store_reader is None:
            return result

        by_pathhash: dict[str, dict[int, list[tuple[str, str]]]] = {}
        for source_token, doc_dir in self.store_reader.iter_document_dirs():
            parsed = parse_source_token(source_token)
            if parsed is None:
                continue
            ph, generation = parsed
            # doc_id = the doc directory's own basename (markdown-first: this
            # must NOT go through a SQLite lookup keyed by source_token — two
            # genuinely distinct duplicate docs can share the same token
            # (that's exactly scenario 3), and a token-keyed lookup would
            # collapse them onto one row instead of surfacing both for
            # isolation.
            doc_id = os.path.basename(doc_dir.rstrip("/"))
            by_pathhash.setdefault(ph, {}).setdefault(generation, []).append((doc_id, doc_dir))

        rows_by_hash = {r["path_hash"]: r for r in self.ledger.all_rows().values()}
        for ph, by_gen in by_pathhash.items():
            row = rows_by_hash.get(ph)
            if row is None:
                continue
            for gen, docs in by_gen.items():
                keep = [(d, dd) for d, dd in docs if not self.ledger.is_cleanup_queued(d)]
                refused = [d for d, dd in docs if self.ledger.is_cleanup_queued(d)]
                result["refused"].extend(refused)
                if not keep:
                    continue
                if len(keep) > 1:
                    result["isolated"].extend(d for d, _ in keep)
                    continue
                doc_id, doc_dir = keep[0]
                if gen == row["active_generation"] and row["active_doc_id"] is None:
                    if self.ledger.adopt_active(row["path"], doc_id, doc_dir):
                        result["adopted_active"].append(doc_id)
                elif (
                    gen == row["generation"]
                    and row["status"] in ("in_flight", "indexing")
                    and row["staging_doc_id"] is None
                ):
                    if self.ledger.adopt_staging(row["path"], doc_id, doc_dir):
                        result["adopted_staging"].append(doc_id)
        return result

    # -- repair (spec §5.5, canonical repair frozen at nit5) -----------------
    def _check_repair_preconditions(self, row, observations, min_interval: float) -> None:
        """Four mechanical preconditions (spec §5.5). M0 single-process
        collapses "no active lease" to trivially-true (no lease modeled) and
        "superstable window" + "two consecutive same-drift reconcile runs"
        into: caller supplies >= 2 timestamped (drift_fingerprint) direct-read
        observations, the last two must agree and be spaced >= min_interval
        apart."""
        reasons = []
        if self.store_reader is not None and self.store_reader.cascade_pending() != 0:
            reasons.append("cascade queue not empty")
        if not observations or len(observations) < 2:
            reasons.append("fewer than two drift observations")
        else:
            (t1, fp1), (t2, fp2) = observations[-2], observations[-1]
            if fp1 != fp2:
                reasons.append("drift fingerprint not stable across last two observations")
            if (t2 - t1) < min_interval:
                reasons.append(f"observations spaced < {min_interval}s apart")
        if reasons:
            raise RepairPreconditionError("; ".join(reasons))

    def repair(
        self,
        path: str,
        *,
        everos_root: str,
        observations: list[tuple[float, str]] | None = None,
        min_interval: float = 0.0,
    ):
        """Canonical repair: delete Lance-derived rows for this doc's nodes,
        force_enqueue the markdown for reprocessing, then re-verify against
        the three stores — all via the injected `repair_runner` (the only
        live/subprocess/env-touching part; this method itself never shells
        out or hardcodes a path). Callers whose repair doesn't converge
        should escalate to delete+recreate; this method reports that but
        does not perform it (a distinct, separately-gated operation)."""
        row = self.ledger.get(path)
        if row is None:
            raise RepairPreconditionError(f"{path}: no ledger row")
        self._check_repair_preconditions(row, observations, min_interval)

        node_ids: list[str] = []
        if self.store_reader is not None and row["active_doc_id"]:
            node_ids = [t.node_id for t in self.store_reader.sqlite_topics(row["active_doc_id"])]

        # fence: reconfirm nothing moved between the precondition check and
        # the destructive step (M0 single-process CAS-lite).
        fresh = self.ledger.get(path)
        if fresh["status"] != row["status"] or fresh["active_doc_id"] != row["active_doc_id"]:
            raise RepairPreconditionError(
                f"{path}: fenced state changed since precondition check, aborting repair"
            )

        if self.repair_runner is None:
            raise RepairPreconditionError(
                f"{path}: no repair_runner injected — cannot execute the canonical repair's "
                "live step (delete Lance rows + force_enqueue under an isolated env)"
            )
        self.repair_runner(path, node_ids, everos_root)

        doc_id = row["active_doc_id"]
        ok, reason = self._verify_staging(row, doc_id) if doc_id else (False, "no active doc")
        outcome = "repaired" if ok else f"repair-failed:{reason}"
        self.ledger.record_attempt(path, "repair", row["generation"], outcome=outcome)
        if ok:
            return {"path": path, "repaired": True}
        return {"path": path, "repaired": False, "reason": reason, "escalate": "delete+recreate"}
