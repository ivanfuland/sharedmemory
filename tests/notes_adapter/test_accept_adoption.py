"""Step 3e / Task 11 reconciliation: accept() (bounded poll over three
stores + digest + coverage, first-round measure-only), demote_for_coverage(),
and adopt_snapshot()'s four scenarios. Uses StoreReader against synthetic
on-disk fixtures matching the REAL EverOS shapes (see
docs/projects/notes-memory/.../task-11-report.md "Shape validation") via
the shared `_store_fixture` helper module.
"""
import hashlib
import sqlite3

import lancedb

from notes_adapter import identity
from notes_adapter.probe.driver import Driver, FakeRemote, IllegalTransition, ScanResult

import pytest

from _store_fixture import write_index_only, write_three_store_fixture


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


_write_three_store_fixture = write_three_store_fixture
_write_index_only = write_index_only


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

    _write_three_store_fixture(store, doc_id, token, 1, "s", "tn", "tp", "Others", 1, "hello world")

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
    _write_three_store_fixture(store, doc_id, token, 1, "s", "tn", "tp", "Others", 1, "hello world")

    # corrupt the real LanceDB row's digest to simulate single-leg drift
    db = lancedb.connect(str(store / ".index" / "lancedb"))
    db.open_table("knowledge_topic").update(
        where=f"id = '{doc_id}_1'", values={"content_sha256": "wrong-digest"}
    )

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
        store, doc_id, wrong_token, 1, "s", "tn", "tp", "Others", 1, "hello world"
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
    _write_three_store_fixture(store, doc_id, token, 1, "s", "tn", "tp", "Others", 1, "hello world")

    # Corrupt the document row's md_path (doc_dir's real source column, see
    # StoreReader.document_row) to point somewhere else than the ledger
    # believes, while the markdown itself stays at the original location.
    conn = sqlite3.connect(store / ".index" / "sqlite" / "system.db")
    conn.execute(
        "UPDATE knowledge_documents SET md_path=? WHERE doc_id=?",
        ("documents/somewhere-else/index.md", doc_id),
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
    _write_three_store_fixture(store, doc_id, token, 1, "s", "tn", "tp", "Others", 1, "x")
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
# max-attempts dead-lettering (T8 review follow-up, Option A)
# ---------------------------------------------------------------------------


def test_permanently_failing_row_dead_letters_after_max_attempts(tmp_path):
    """T8 follow-up (Option A): a row whose accept() never succeeds must not
    retry forever. After MAX_GENERATION_ATTEMPTS (3) failed attempts in the
    same generation, run_once() dead-letters it (failed -> dead) instead of
    retrying a 4th time.

    Before this fix, a permanently-failing row could starve an unrelated
    healthy pending row indefinitely: _claim_next() always prioritizes
    indexing rows over pending/failed rows, and a.md kept re-entering
    indexing on every retry -- so b.md (pending) never got a turn while
    a.md was alive in the pending/failed/indexing rotation. This test
    proves both halves of the fix: (1) the bad row terminalizes at exactly
    3 attempts and is never reclaimed afterward, and (2) once it does, the
    previously-starved healthy row makes it all the way to `synced`, and
    the probe eventually reaches a genuine no-op turn (run_once() -> None)
    instead of looping forever."""
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("always fails")
    (vault / "b.md").write_text("healthy")

    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md", "b.md"], vault))
    assert d.row("a.md")["status"] == "pending"
    assert d.row("b.md")["status"] == "pending"

    # a.md never gets a store fixture written, so accept() can never verify
    # it -- every attempt ends in indexing -> failed. Drive exactly 3 fail
    # cycles (post -> accept-fails -> failed), 2 run_once() calls each.
    for attempt in range(1, 4):
        r1 = d.run_once()  # a.md: (failed->)pending -> in_flight -> indexing
        assert r1["path"] == "a.md" and r1["status"] == "indexing"
        r2 = d.run_once()  # a.md: indexing -> accept-fails -> failed
        assert r2["path"] == "a.md" and r2["status"] == "failed"
        assert d.row("a.md")["generation_attempts"] == attempt
        # b.md is a healthy pending row sitting right next to a.md the
        # whole time -- it does not get a turn while a.md is still alive
        # (indexing outranks pending in _claim_next()), which is exactly
        # the starvation this dead-letter fix resolves once a.md dies.
        assert d.row("b.md")["status"] == "pending"

    # 4th reclaim of a.md: MAX_GENERATION_ATTEMPTS (3) already reached ->
    # dead-lettered instead of retried a 4th time.
    last_error_before_dead = d.row("a.md")["last_error"]
    r3 = d.run_once()
    assert r3["path"] == "a.md" and r3["status"] == "dead"
    assert r3["reason"] == last_error_before_dead
    row_a = d.row("a.md")
    assert row_a["status"] == "dead"
    assert row_a["dead_reason"] == last_error_before_dead
    assert row_a["generation_attempts"] == 3  # not bumped again on dead-letter

    # a.md is TERMINAL: with it excluded from _claim_next(), the
    # previously-starved b.md finally gets its turn -- proving the
    # dead-letter is what unblocks it, not luck.
    r4 = d.run_once()  # b.md: pending -> in_flight -> indexing
    assert r4["path"] == "b.md" and r4["status"] == "indexing"
    assert d.row("a.md")["status"] == "dead"  # untouched, never reclaimed

    row_b = d.row("b.md")
    token = identity.source_token("b.md", row_b["generation"])
    doc_id = row_b["staging_doc_id"]
    _write_three_store_fixture(store, doc_id, token, 1, "s", "tn", "tp", "Others", 1, "healthy")
    r5 = d.run_once()  # b.md: indexing -> accept -> synced
    assert r5["path"] == "b.md" and r5["status"] == "synced"

    # Nothing left to do: a.md is dead (excluded forever), b.md is synced
    # (also excluded from claiming). The probe genuinely terminates instead
    # of looping on the dead row.
    assert d.run_once() is None
    assert d.row("a.md")["status"] == "dead"  # still never reclaimed


def test_dead_row_revives_on_new_generation(tmp_path):
    """T8 follow-up: a dead-lettered row is not gone forever. If the file it
    tracks genuinely changes (an edit, not a ghost retry), the next
    whitelist scan revives dead -> pending at a new generation with
    generation_attempts reset to 0, so it gets a fresh
    MAX_GENERATION_ATTEMPTS budget. Mirrors the existing synced -> pending
    "new generation picked up by a scan" path, but starting from the
    terminal `dead` status instead of `synced`."""
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("always fails v1")

    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md"], vault))
    for _ in range(3):
        d.run_once()  # -> indexing
        d.run_once()  # -> accept-fails -> failed
    assert d.row("a.md")["generation_attempts"] == 3
    d.run_once()  # 4th reclaim: dead-letters
    row = d.row("a.md")
    assert row["status"] == "dead"
    assert row["dead_reason"]
    old_generation = row["generation"]

    # Edit the file -- the next whitelist scan must revive it.
    (vault / "a.md").write_text("edited content, deserves a fresh attempt budget")
    diff = d.scan_result(mk_scan(["a.md"], vault))
    assert "a.md" in diff["updated"]
    row2 = d.row("a.md")
    assert row2["status"] == "pending"
    assert row2["generation"] == old_generation + 1
    assert row2["generation_attempts"] == 0
    assert row2["dead_reason"] is None
    assert row2["operation"] == "create"  # this path never had an active_doc_id

    # And it can retry cleanly: write the store fixture this time and let
    # it succeed on the very first attempt of the new generation.
    d.run_once()  # pending -> in_flight -> indexing
    row3 = d.row("a.md")
    token = identity.source_token("a.md", row3["generation"])
    doc_id = row3["staging_doc_id"]
    _write_three_store_fixture(
        store, doc_id, token, 1, "s", "tn", "tp", "Others", 1,
        "edited content, deserves a fresh attempt budget",
    )
    d.run_once()  # indexing -> accept -> synced
    row4 = d.row("a.md")
    assert row4["status"] == "synced"
    assert row4["generation_attempts"] == 0  # never had to fail this generation


def test_failed_row_recovers_within_max_attempts_still_reaches_synced(tmp_path):
    """T8 follow-up regression guard: dead-lettering must only kick in once
    MAX_GENERATION_ATTEMPTS is truly exhausted. A row that fails twice
    (generation_attempts stays below 3) and then succeeds on the third try
    must still reach `synced` via the pre-existing failed -> pending retry
    path (spec'd by test_failed_row_retries_without_blocking_unrelated_
    pending_row), not get dead-lettered early."""
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("eventually ok")

    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md"], vault))

    # Two genuine failures (no store fixture written yet) -- must retry via
    # failed -> pending, not dead-letter (2 < MAX_GENERATION_ATTEMPTS=3).
    for _ in range(2):
        d.run_once()  # -> indexing
        d.run_once()  # -> accept-fails -> failed
    row = d.row("a.md")
    assert row["status"] == "failed"
    assert row["generation_attempts"] == 2

    # Third attempt: write the store fixture before accept() runs, so this
    # retry actually succeeds.
    d.run_once()  # failed -> pending -> in_flight -> indexing
    row = d.row("a.md")
    assert row["status"] == "indexing"
    token = identity.source_token("a.md", row["generation"])
    doc_id = row["staging_doc_id"]
    _write_three_store_fixture(store, doc_id, token, 1, "s", "tn", "tp", "Others", 1, "eventually ok")
    d.run_once()  # indexing -> accept -> synced
    row = d.row("a.md")
    assert row["status"] == "synced"
    assert row["generation_attempts"] == 2  # unchanged by a successful accept


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
    _write_three_store_fixture(store, doc_id, token, 1, "s", "tn", "tp", "Others", 1, "hello world")
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
