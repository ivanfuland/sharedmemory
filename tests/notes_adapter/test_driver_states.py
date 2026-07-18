"""Task 8 core: ledger state machine + scanner + driver two-primitive flow.

No live EverOS instance, no network — FakeRemote is the in-memory test
double for every state-machine test in this file. Real-store / real-HTTP
contract tests live in test_http_remote.py / test_store_reader.py; live
integration is Task 11.
"""
import hashlib
import os
import stat

import pytest

from notes_adapter import identity
from notes_adapter.probe.driver import (
    Driver,
    FakeRemote,
    IllegalTransition,
    ScanResult,
    Scanner,
)


def mk_scan(paths: list[str], root) -> ScanResult:
    """Test-only ScanResult producer: reads each relpath directly under
    `root` and builds the SampleRef four-tuple identity.py defines, mirroring
    what Scanner.scan() would produce for those specific files."""
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


# ---------------------------------------------------------------------------
# Step 1: state-machine tests (verbatim from the task brief)
# ---------------------------------------------------------------------------


def test_create_then_synced(tmp_path):
    (tmp_path / "a.md").write_text("---\ntitle: t\n---\nbody")
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    d.scan_result(mk_scan(["a.md"], tmp_path))
    d.run_once()  # pending -> in_flight -> indexing (POST staging create)
    d.run_once()  # accept -> synced (FakeRemote is immediately consistent)
    row = d.row("a.md")
    assert row["status"] == "synced" and row["active_doc_id"]


def test_update_is_staging_promote(tmp_path):
    (tmp_path / "a.md").write_text("---\ntitle: t\n---\nv1")
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    d.scan_result(mk_scan(["a.md"], tmp_path))
    d.run_once()
    d.run_once()
    old = d.row("a.md")["active_doc_id"]

    (tmp_path / "a.md").write_text("---\ntitle: t\n---\nv2")
    d.scan(["a.md"])  # generation+1, operation=update
    d.run_once()  # staging create
    assert d.row("a.md")["active_doc_id"] == old  # old active untouched

    d.run_once()  # accept + promote + old into cleanup
    row = d.row("a.md")
    assert row["active_doc_id"] != old and row["status"] == "synced"
    assert old in d.remote.cleanup_requested

    d.run_cleanup()
    assert old not in d.remote.docs  # R3 P0-1: old generation is really deleted, not just queued


def test_delete_two_scan_confirm(tmp_path):
    (tmp_path / "a.md").write_text("x")
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    d.scan_result(mk_scan(["a.md"], tmp_path))
    d.run_once()
    d.run_once()

    d.scan_result(mk_scan([], tmp_path))  # miss 1 -> streak=1, no delete
    assert d.row("a.md")["status"] == "synced"

    d.scan_result(mk_scan([], tmp_path))
    d.run_once()  # miss 2 -> delete
    assert d.row("a.md")["status"] == "deleted"


class _NoopDeleteRemote(FakeRemote):
    """FakeRemote whose delete() silently no-ops -- the doc stays in
    self.docs, simulating a remote DELETE that returns success without
    actually removing anything (or a transient non-raising failure)."""

    def delete(self, doc_id: str) -> int:
        return 0


def test_complete_delete_does_not_mark_deleted_when_remote_delete_noops(tmp_path):
    """Important (T8 review): unlike run_cleanup() (which checks
    still_present before marking a cleanup job done), _complete_delete()
    used to call remote.delete(doc_id) and unconditionally transition
    retiring->deleted regardless of result -- violating spec §5.4 step 7
    (confirm-before-terminal-state). A delete() that silently no-ops must
    NOT be recorded as deleted while the doc may still be live."""
    (tmp_path / "a.md").write_text("x")
    d = Driver(":memory:", _NoopDeleteRemote(), str(tmp_path))
    d.scan_result(mk_scan(["a.md"], tmp_path))
    d.run_once()
    d.run_once()
    assert d.row("a.md")["status"] == "synced"
    doc_id = d.row("a.md")["active_doc_id"]

    d.scan_result(mk_scan([], tmp_path))  # miss 1 -> streak=1
    d.scan_result(mk_scan([], tmp_path))  # miss 2 -> retiring
    assert d.row("a.md")["status"] == "retiring"

    result = d.run_once()  # attempts delete; remote no-ops -- must not reach deleted
    assert d.row("a.md")["status"] != "deleted"
    assert d.row("a.md")["status"] == "retiring"
    assert result["status"] == "retiring"
    assert result["confirmed"] is False
    assert doc_id in d.remote.docs  # still present -- not silently dropped


# ---------------------------------------------------------------------------
# Step 3a: schema + transition()
# ---------------------------------------------------------------------------


def test_illegal_transition_rejected(tmp_path):
    (tmp_path / "a.md").write_text("x")
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    d.scan_result(mk_scan(["a.md"], tmp_path))
    d.run_once()
    d.run_once()
    assert d.row("a.md")["status"] == "synced"
    # synced -> indexing directly (skipping pending/in_flight, no new
    # generation) must be rejected — the only legal path to indexing is
    # synced -> pending (new gen) -> in_flight -> indexing.
    with pytest.raises(IllegalTransition):
        d.ledger.transition("a.md", "synced", "indexing", staging_doc_id="x")


def test_transition_rejects_stale_from_status(tmp_path):
    (tmp_path / "a.md").write_text("x")
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    d.scan_result(mk_scan(["a.md"], tmp_path))
    # row is 'pending'; claiming it as if it were 'failed' must be rejected
    with pytest.raises(IllegalTransition):
        d.ledger.transition("a.md", "failed", "pending")


def test_transition_unknown_pair_rejected_without_touching_row(tmp_path):
    (tmp_path / "a.md").write_text("x")
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    d.scan_result(mk_scan(["a.md"], tmp_path))
    with pytest.raises(IllegalTransition):
        d.ledger.transition("a.md", "pending", "synced")  # not in the allowed table
    assert d.row("a.md")["status"] == "pending"  # rejected transition left row untouched


# ---------------------------------------------------------------------------
# Step 3b: Scanner (canary / glob / safe-open) + missing_streak semantics
# ---------------------------------------------------------------------------


def _mk_vault(tmp_path, sentinel=".m0-root-sentinel"):
    (tmp_path / sentinel).write_text("ok")
    (tmp_path / "notes").mkdir()
    return tmp_path


def test_scanner_missing_root_sentinel_ok_false(tmp_path):
    (tmp_path / "notes").mkdir()  # no sentinel file at all
    sr = Scanner().scan(str(tmp_path), ["notes"])
    assert sr.ok is False and sr.refs == {}


def test_scanner_missing_root_dir_ok_false(tmp_path):
    missing = tmp_path / "does-not-exist"
    sr = Scanner().scan(str(missing), ["notes"])
    assert sr.ok is False and sr.refs == {}


def test_scanner_healthy_empty_dir_ok_true_zero_files(tmp_path):
    _mk_vault(tmp_path)
    sr = Scanner().scan(str(tmp_path), ["notes"])
    assert sr.ok is True and sr.refs == {}


def test_scanner_chmod_000_dir_ok_false_zero_diff(tmp_path):
    _mk_vault(tmp_path)
    blocked = tmp_path / "notes" / "locked"
    blocked.mkdir()
    (blocked / "a.md").write_text("x")
    os.chmod(blocked, 0o000)
    try:
        sr = Scanner().scan(str(tmp_path), ["notes"])
        assert sr.ok is False and sr.refs == {}
    finally:
        os.chmod(blocked, stat.S_IRWXU)  # restore so tmp_path cleanup can remove it


def test_scanner_finds_md_files_with_hash(tmp_path):
    _mk_vault(tmp_path)
    (tmp_path / "notes" / "a.md").write_text("hello")
    sr = Scanner().scan(str(tmp_path), ["notes"])
    assert sr.ok is True
    assert "notes/a.md" in sr.refs
    ref = sr.refs["notes/a.md"]
    assert ref.vault_raw_hash == identity.content_hash(b"hello")
    assert ref.staged_sha256 == hashlib.sha256(b"hello").hexdigest()


def test_healthy_empty_dir_two_rounds_reaches_delete_confirm(tmp_path):
    """Scanner-driven integration: a file synced once, then the vault goes
    to a healthy-but-empty state for two scan rounds -> two-scan delete
    confirm fires exactly like the FakeRemote-refs version in Step 1."""
    _mk_vault(tmp_path)
    (tmp_path / "notes" / "a.md").write_text("x")
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    d.scan_whitelist(["notes"])
    d.run_once()
    d.run_once()
    assert d.row("notes/a.md")["status"] == "synced"

    (tmp_path / "notes" / "a.md").unlink()
    sr1 = d.scanner.scan(str(tmp_path), ["notes"])
    assert sr1.ok is True and sr1.refs == {}
    d.scan_result(sr1)
    assert d.row("notes/a.md")["status"] == "synced"  # streak=1, not yet deleted

    sr2 = d.scanner.scan(str(tmp_path), ["notes"])
    d.scan_result(sr2)
    d.run_once()
    assert d.row("notes/a.md")["status"] == "deleted"


def test_scanner_ok_false_does_not_advance_missing_streak(tmp_path):
    _mk_vault(tmp_path)
    (tmp_path / "notes" / "a.md").write_text("x")
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    d.scan_whitelist(["notes"])
    d.run_once()
    d.run_once()
    assert d.row("notes/a.md")["status"] == "synced"
    assert d.row("notes/a.md")["missing_streak"] == 0

    # break the mount (remove sentinel) then rescan with the file physically
    # gone too -- ok=False must NOT bump missing_streak even though the file
    # is also absent.
    (tmp_path / "notes" / "a.md").unlink()
    (tmp_path / ".m0-root-sentinel").unlink()
    sr = d.scanner.scan(str(tmp_path), ["notes"])
    assert sr.ok is False
    d.scan_result(sr)
    assert d.row("notes/a.md")["missing_streak"] == 0
    assert d.row("notes/a.md")["status"] == "synced"


# ---------------------------------------------------------------------------
# Step 3f2: cleanup executor
# ---------------------------------------------------------------------------


def test_run_cleanup_removes_old_generation_from_all_faces(tmp_path):
    (tmp_path / "a.md").write_text("v1")
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    d.scan_result(mk_scan(["a.md"], tmp_path))
    d.run_once()
    d.run_once()
    old = d.row("a.md")["active_doc_id"]

    (tmp_path / "a.md").write_text("v2")
    d.scan(["a.md"])
    d.run_once()
    d.run_once()

    jobs_before = d.ledger.pending_cleanup_jobs()
    assert len(jobs_before) == 1 and jobs_before[0]["doc_id"] == old

    results = d.run_cleanup()
    assert results == [{"doc_id": old, "done": True}]
    assert old not in d.remote.docs
    assert d.ledger.pending_cleanup_jobs() == []


def test_run_cleanup_is_idempotent_on_empty_queue(tmp_path):
    (tmp_path / "a.md").write_text("x")
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    assert d.run_cleanup() == []


def test_tombstone_revival_unconditional_create_new_generation(tmp_path):
    (tmp_path / "a.md").write_text("x")
    d = Driver(":memory:", FakeRemote(), str(tmp_path))
    d.scan_result(mk_scan(["a.md"], tmp_path))
    d.run_once()
    d.run_once()
    d.scan_result(mk_scan([], tmp_path))
    d.scan_result(mk_scan([], tmp_path))
    d.run_once()
    assert d.row("a.md")["status"] == "deleted"
    old_gen = d.row("a.md")["generation"]

    (tmp_path / "a.md").write_text("reborn")
    d.scan_result(mk_scan(["a.md"], tmp_path))
    row = d.row("a.md")
    assert row["status"] == "pending" and row["generation"] == old_gen + 1
