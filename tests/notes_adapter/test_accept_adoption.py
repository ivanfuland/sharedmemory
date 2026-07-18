"""Step 3e: accept() (bounded poll over three stores + digest + coverage,
first-round measure-only), demote_for_coverage(), and adopt_snapshot()'s four
scenarios. Uses StoreReader against synthetic on-disk fixtures (constructed
here) — never a real EverOS store (Task 11's job).
"""
import hashlib
import json
import sqlite3

from notes_adapter import identity
from notes_adapter.digest import canonical_digest
from notes_adapter.probe.driver import Driver, FakeRemote, IllegalTransition, ScanResult

import pytest


def mk_scan(paths, root):
    """Same ScanResult producer as test_driver_states.py's mk_scan — kept
    local rather than cross-imported so each test file stays self-contained."""
    refs = {}
    for rel in paths:
        data = (root / rel).read_bytes()
        cpath = identity.canonical_path(rel)
        refs[cpath] = identity.SampleRef(
            canonical_path=cpath,
            staged_relpath=rel,
            vault_raw_hash=identity.content_hash(data),
            staged_sha256=hashlib.sha256(data).hexdigest(),
        )
    return ScanResult(ok=True, refs=refs)


class _RelDirFakeRemote(FakeRemote):
    """FakeRemote returning a store-relative doc_dir (documents/<id>),
    matching the on-disk StoreReader fixture layout these tests build."""

    def post(self, doc):
        self._seq += 1
        doc_id = f"fake-doc-{self._seq}"
        doc_dir = f"documents/{doc_id}"
        self.docs[doc_id] = {"source_token": doc.source_token, "doc_dir": doc_dir, "topic_count": 1}
        return doc_id, doc_dir, 1


def _write_three_store_fixture(
    store_root, doc_id, source_token, node_id, summary, topic_name, topic_path, category_id, depth, content
):
    conn = sqlite3.connect(store_root / "system.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_documents (doc_id TEXT PRIMARY KEY, source_token TEXT, doc_dir TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_topics (node_id TEXT, doc_id TEXT, summary TEXT, "
        "topic_name TEXT, topic_path TEXT, category_id TEXT, depth INT, content TEXT, content_sha256 TEXT)"
    )
    doc_dir = f"documents/{doc_id}"
    conn.execute("INSERT OR REPLACE INTO knowledge_documents VALUES (?,?,?)", (doc_id, source_token, doc_dir))
    conn.execute(
        "INSERT INTO knowledge_topics VALUES (?,?,?,?,?,?,?,?,?)",
        (node_id, doc_id, summary, topic_name, topic_path, category_id, depth, content,
         hashlib.sha256(content.encode()).hexdigest()),
    )
    conn.commit()
    conn.close()

    (store_root / "lance").mkdir(exist_ok=True)
    digest = canonical_digest(summary, topic_name, topic_path, category_id, depth, content)
    (store_root / "lance" / f"{doc_id}.json").write_text(json.dumps({node_id: digest}))

    ddir = store_root / doc_dir
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "index.md").write_text(f"---\nsource_token: {source_token}\n---\n")
    md = (
        f"---\nsummary: {summary}\ntopic_name: {topic_name}\ntopic_path: {topic_path}\n"
        f"category_id: {category_id}\ndepth: {depth}\n---\n{content}"
    )
    (ddir / f"{node_id}.md").write_text(md)
    return doc_dir


def _write_index_only(store_root, doc_id, source_token):
    conn = sqlite3.connect(store_root / "system.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_documents (doc_id TEXT PRIMARY KEY, source_token TEXT, doc_dir TEXT)"
    )
    doc_dir = f"documents/{doc_id}"
    conn.execute("INSERT OR REPLACE INTO knowledge_documents VALUES (?,?,?)", (doc_id, source_token, doc_dir))
    conn.commit()
    conn.close()
    ddir = store_root / doc_dir
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "index.md").write_text(f"---\nsource_token: {source_token}\n---\n")
    return doc_dir


# ---------------------------------------------------------------------------
# accept()
# ---------------------------------------------------------------------------


def test_accept_succeeds_with_consistent_three_stores(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("---\ntitle: t\n---\nhello world")

    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md"], vault))
    d.run_once()  # -> indexing
    row = d.row("a.md")
    doc_id = row["staging_doc_id"]
    token = identity.source_token("a.md", row["generation"])

    _write_three_store_fixture(store, doc_id, token, "n1", "s", "tn", "tp", "Others", 1, "hello world")

    ok, reason = d.accept("a.md")
    assert ok is True and reason is None


def test_accept_fails_when_document_row_missing(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("x")

    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md"], vault))
    d.run_once()

    ok, reason = d.accept("a.md", max_attempts=2)
    assert ok is False and "document row missing" in reason


def test_accept_fails_when_topic_digest_drifts(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("---\ntitle: t\n---\nhello world")

    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md"], vault))
    d.run_once()
    row = d.row("a.md")
    doc_id = row["staging_doc_id"]
    token = identity.source_token("a.md", row["generation"])
    _write_three_store_fixture(store, doc_id, token, "n1", "s", "tn", "tp", "Others", 1, "hello world")

    # corrupt the Lance sidecar digest to simulate single-leg drift
    (store / "lance" / f"{doc_id}.json").write_text(json.dumps({"n1": "wrong-digest"}))

    ok, reason = d.accept("a.md", max_attempts=1)
    assert ok is False and "verify_rows" in reason


def test_accept_fails_when_source_token_diverges_from_ledger_expectation(tmp_path):
    """R5 #P0-4 (Important, T8 review): a knowledge_documents row can exist
    for `doc_id` yet not actually be *this* attempt's document -- document
    and topic are independent cascade handlers that can diverge. A
    source_token belonging to a different generation than the ledger's
    current intent must fail verification, not silently pass."""
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("---\ntitle: t\n---\nhello world")

    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md"], vault))
    d.run_once()
    row = d.row("a.md")
    doc_id = row["staging_doc_id"]

    wrong_token = identity.source_token("a.md", row["generation"] + 1)
    _write_three_store_fixture(
        store, doc_id, wrong_token, "n1", "s", "tn", "tp", "Others", 1, "hello world"
    )

    ok, reason = d.accept("a.md", max_attempts=1)
    assert ok is False
    assert "source_token mismatch" in reason


def test_accept_fails_when_doc_dir_diverges_from_ledger_expectation(tmp_path):
    """R5 #P0-4 (Important, T8 review): SQLite's doc_dir column is the
    authoritative location, but it must agree with what the ledger believes
    it posted to. If they diverge, accept() must surface the divergence
    rather than silently reading the ledger-believed (possibly wrong)
    location."""
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("---\ntitle: t\n---\nhello world")

    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md"], vault))
    d.run_once()
    row = d.row("a.md")
    doc_id = row["staging_doc_id"]
    token = identity.source_token("a.md", row["generation"])
    _write_three_store_fixture(store, doc_id, token, "n1", "s", "tn", "tp", "Others", 1, "hello world")

    # Corrupt the document row's doc_dir to point somewhere else than the
    # ledger believes, while the markdown itself stays at the original
    # location.
    conn = sqlite3.connect(store / "system.db")
    conn.execute(
        "UPDATE knowledge_documents SET doc_dir=? WHERE doc_id=?",
        ("documents/somewhere-else", doc_id),
    )
    conn.commit()
    conn.close()

    ok, reason = d.accept("a.md", max_attempts=1)
    assert ok is False
    assert "doc_dir mismatch" in reason


def test_accept_raises_outside_indexing_status(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("x")
    d = Driver(":memory:", FakeRemote(), str(vault))
    d.scan_result(mk_scan(["a.md"], vault))  # status is 'pending', not 'indexing'
    with pytest.raises(IllegalTransition):
        d.accept("a.md")


def test_failed_row_retries_without_blocking_unrelated_pending_row(tmp_path):
    """Regression for the CRITICAL state-machine bug (T8 review): a `failed`
    row used to make run_once() raise IllegalTransition uncaught, because
    `_claim_and_post` unconditionally attempted `transition(path, "failed",
    "in_flight", ...)` -- not in `_ALLOWED_TRANSITIONS` (only
    ("failed","pending") is). Since `_claim_next()` claims pending and
    failed together, a single failed row crashed run_once() even with a
    healthy, unrelated pending row sitting right next to it -- livelocking
    the whole ledger. Fix: retry re-picks-up via the existing
    failed->pending edge before proceeding pending->in_flight."""
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("x")

    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md"], vault))
    d.run_once()  # a.md: pending -> in_flight -> indexing
    d.run_once()  # accept exhausts retries (no store fixture written) -> failed
    assert d.row("a.md")["status"] == "failed"

    # Second, unrelated pending row in the same ledger.
    (vault / "b.md").write_text("y")
    d.scan_result(mk_scan(["a.md", "b.md"], vault))
    assert d.row("a.md")["status"] == "failed"  # untouched by the rescan
    assert d.row("b.md")["status"] == "pending"

    # This call used to raise IllegalTransition and crash -- that is exactly
    # the bug this test guards against.
    result = d.run_once()
    row_a = d.row("a.md")
    assert result is not None
    assert row_a["status"] == "indexing"  # failed -> pending -> in_flight -> indexing, one turn

    # Let the retried attempt actually succeed this time.
    token = identity.source_token("a.md", row_a["generation"])
    doc_id = row_a["staging_doc_id"]
    _write_three_store_fixture(store, doc_id, token, "n1", "s", "tn", "tp", "Others", 1, "x")
    d.run_once()  # a.md: indexing -> accept -> synced
    assert d.row("a.md")["status"] == "synced"

    # The unrelated pending row must still be progressable -- proves the
    # failed row never blocked it.
    d.run_once()  # b.md: pending -> in_flight -> indexing
    assert d.row("b.md")["status"] == "indexing"


def test_run_once_demotes_to_failed_when_accept_never_succeeds(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("x")
    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md"], vault))
    d.run_once()  # -> indexing; no store fixture written, so accept() will never verify
    d.run_once()  # -> accept exhausts retries -> failed
    row = d.row("a.md")
    assert row["status"] == "failed"
    assert row["staging_doc_id"] is None


# ---------------------------------------------------------------------------
# demote_for_coverage()
# ---------------------------------------------------------------------------


def test_demote_for_coverage_moves_synced_to_failed_and_clears_active(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("---\ntitle: t\n---\nhello world")
    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md"], vault))
    d.run_once()
    row = d.row("a.md")
    doc_id = row["staging_doc_id"]
    token = identity.source_token("a.md", row["generation"])
    _write_three_store_fixture(store, doc_id, token, "n1", "s", "tn", "tp", "Others", 1, "hello world")
    d.run_once()  # -> synced
    assert d.row("a.md")["status"] == "synced"

    result = d.demote_for_coverage("a.md")
    assert result["status"] == "failed" and result["reason"] == "coverage"
    row = d.row("a.md")
    assert row["status"] == "failed"
    assert row["active_doc_id"] is None
    assert doc_id not in d.remote.docs


def test_demote_for_coverage_rejects_non_synced_row(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("x")
    d = Driver(":memory:", FakeRemote(), str(vault))
    d.scan_result(mk_scan(["a.md"], vault))  # status = pending
    with pytest.raises(IllegalTransition):
        d.demote_for_coverage("a.md")


# ---------------------------------------------------------------------------
# adopt_snapshot(): four scenarios (R8 P1-7)
# ---------------------------------------------------------------------------


def _insert_raw_row(d, path, **fields):
    cols = ["path", "path_hash"] + list(fields.keys())
    vals = [path, identity.path_hash(path)] + list(fields.values())
    placeholders = ",".join("?" for _ in vals)
    d.ledger._conn.execute(
        f"INSERT INTO notes_ledger ({', '.join(cols)}) VALUES ({placeholders})", vals
    )
    d.ledger._conn.commit()


def test_adopt_snapshot_scenario1_active_generation_adopted(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    d = Driver(":memory:", FakeRemote(), str(tmp_path / "vault"), memory_root=str(store))
    _insert_raw_row(
        d, "a.md", desired_hash="h", desired_fingerprint="fp", operation="none",
        status="synced", generation=0, active_generation=0, missing_streak=0,
    )
    token = identity.source_token("a.md", 0)
    _write_index_only(store, "doc-active", token)

    result = d.adopt_snapshot()
    assert result["adopted_active"] == ["doc-active"]
    row = d.row("a.md")
    assert row["active_doc_id"] == "doc-active"
    assert row["active_doc_dir"].endswith("documents/doc-active")


def test_adopt_snapshot_scenario2_current_intent_adopted_into_staging_only(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    d = Driver(":memory:", FakeRemote(), str(tmp_path / "vault"), memory_root=str(store))
    _insert_raw_row(
        d, "a.md", desired_hash="h2", desired_fingerprint="fp2", operation="update",
        status="indexing", generation=1, active_generation=0, active_doc_id="doc-old",
        active_doc_dir="documents/doc-old", missing_streak=0,
    )
    token = identity.source_token("a.md", 1)
    _write_index_only(store, "doc-new", token)

    result = d.adopt_snapshot()
    assert result["adopted_staging"] == ["doc-new"]
    row = d.row("a.md")
    assert row["staging_doc_id"] == "doc-new"
    assert row["active_doc_id"] == "doc-old"  # active untouched — promotion still requires accept


def test_adopt_snapshot_scenario3_same_generation_duplicates_isolated(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    d = Driver(":memory:", FakeRemote(), str(tmp_path / "vault"), memory_root=str(store))
    _insert_raw_row(
        d, "a.md", desired_hash="h", desired_fingerprint="fp", operation="none",
        status="synced", generation=0, active_generation=0, missing_streak=0,
    )
    token = identity.source_token("a.md", 0)
    _write_index_only(store, "doc-dup-1", token)
    _write_index_only(store, "doc-dup-2", token)

    result = d.adopt_snapshot()
    assert set(result["isolated"]) == {"doc-dup-1", "doc-dup-2"}
    assert d.row("a.md")["active_doc_id"] is None  # no winner picked


def test_adopt_snapshot_scenario4_cleanup_queued_doc_refused(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    d = Driver(":memory:", FakeRemote(), str(tmp_path / "vault"), memory_root=str(store))
    _insert_raw_row(
        d, "a.md", desired_hash="h", desired_fingerprint="fp", operation="none",
        status="synced", generation=0, active_generation=0, missing_streak=0,
    )
    token = identity.source_token("a.md", 0)
    _write_index_only(store, "doc-garbage", token)
    d.ledger.enqueue_cleanup("doc-garbage", src_path="a.md", src_generation=0, reason="test-precondition")

    result = d.adopt_snapshot()
    assert result["refused"] == ["doc-garbage"]
    assert result["adopted_active"] == []
    assert d.row("a.md")["active_doc_id"] is None  # never resurrected


def test_adopt_snapshot_no_store_reader_is_noop(tmp_path):
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    result = d.adopt_snapshot()
    assert result == {"adopted_active": [], "adopted_staging": [], "isolated": [], "refused": []}
