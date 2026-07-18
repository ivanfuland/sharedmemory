"""Task 10 Step 4.5: lookup_core — eligibility() reuse (spec §8 matrix, one
test per matrix row) + lookup()/body() TDD (state-exclusion / date-filter /
over-length-truncation / stale) + one privacy negative test (seeded secret
must come out redacted). No live EverOS instance — this is pure ledger/vault
logic, same style as test_search_core.py.
"""
import pytest

from notes_adapter.lookup_core import body, lookup
from notes_adapter.search_core import eligibility


def _row(**overrides):
    row = {
        "admitted": True,
        "active_doc_id": "doc-1",
        "operation": "none",
        "status": "synced",
        "synced_frontmatter": {},
        "synced_hash": None,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# eligibility() reused from search_core — one test per spec §8 matrix row.
# (search_core's own suite covers the same function from search()'s angle;
# these assert the *lookup*-facing axes explicitly, per R3 P1-10: shared
# function, not a forked copy.)
# ---------------------------------------------------------------------------


def test_matrix_row_synced():
    e = eligibility("none", "doc-1", True, "synced")
    assert e.lookup_default_visible and e.body_allowed and not e.stale


def test_matrix_row_update_inflight_old_active():
    for status in ("pending", "in_flight", "indexing", "failed"):
        e = eligibility("update", "doc-1", True, status)
        assert e.lookup_default_visible and e.body_allowed and e.stale, status


def test_matrix_row_first_create_no_active():
    e = eligibility("create", None, True, "pending")
    assert not e.lookup_default_visible
    assert e.lookup_include_status_visible and not e.body_allowed


def test_matrix_row_delete_intent_confirmed():
    for status in ("pending", "in_flight"):
        e = eligibility("delete", "doc-1", True, status)
        assert not e.lookup_default_visible
        assert e.lookup_include_status_visible and not e.body_allowed, status


def test_matrix_row_pending_repair():
    e = eligibility("none", "doc-1", True, "pending_repair")
    assert not e.lookup_default_visible
    assert e.lookup_include_status_visible and not e.body_allowed


def test_matrix_row_retiring():
    e = eligibility("none", "doc-1", True, "retiring")
    assert not e.lookup_default_visible
    assert e.lookup_include_status_visible and not e.body_allowed


def test_matrix_row_deleted():
    e = eligibility("none", None, True, "deleted")
    assert not e.lookup_default_visible
    assert e.lookup_include_status_visible and not e.body_allowed


def test_matrix_row_dead():
    e = eligibility("none", None, True, "dead")
    assert not e.lookup_default_visible
    assert e.lookup_include_status_visible and not e.body_allowed


# ---------------------------------------------------------------------------
# lookup() / body() TDD: state-exclusion / date-filter / over-length-truncation / stale
# ---------------------------------------------------------------------------


def test_lookup_state_exclusion(tmp_path):
    (tmp_path / "a.md").write_text("body a")
    ledger = {"a.md": _row(status="dead", active_doc_id=None)}
    rows = lookup(str(tmp_path), ledger, paths=["a.md"])
    assert rows == []


def test_lookup_state_exclusion_visible_with_include_status(tmp_path):
    (tmp_path / "a.md").write_text("body a")
    ledger = {"a.md": _row(status="dead", active_doc_id=None)}
    rows = lookup(str(tmp_path), ledger, paths=["a.md"], include_status=True)
    assert len(rows) == 1 and rows[0]["content"] is None  # visible metadata, no body


def test_lookup_date_filter(tmp_path):
    (tmp_path / "in.md").write_text("in range")
    (tmp_path / "out.md").write_text("out of range")
    ledger = {
        "in.md": _row(synced_frontmatter={"date": "2026-03-15"}),
        "out.md": _row(synced_frontmatter={"date": "2025-01-01"}),
    }
    rows = lookup(str(tmp_path), ledger, date_range=("2026-01-01", "2026-12-31"))
    assert [r["path"] for r in rows] == ["in.md"]


def test_lookup_body_truncates_over_length(tmp_path):
    long_text = "x" * 5000
    (tmp_path / "long.md").write_text(long_text)
    ledger = {"long.md": _row()}
    rows = lookup(str(tmp_path), ledger, paths=["long.md"], want_body=True, max_chars=4000)
    assert rows[0]["content_truncated"] is True
    assert len(rows[0]["content"]) == 4000


def test_lookup_body_not_truncated_under_cap(tmp_path):
    (tmp_path / "short.md").write_text("short body")
    ledger = {"short.md": _row()}
    rows = lookup(str(tmp_path), ledger, paths=["short.md"], want_body=True, max_chars=4000)
    assert rows[0]["content_truncated"] is False
    assert rows[0]["content"] == "short body"


def test_lookup_stale_marker_set_even_when_hash_unchanged(tmp_path):
    """A row mid-update still serving its old active doc is 'stale' by sync
    state alone, independent of whether the live file bytes still match
    `synced_hash` — spec §8's stale flag is about which generation is being
    served, not about content drift."""
    from notes_adapter import identity

    text = "unchanged since sync"
    (tmp_path / "a.md").write_text(text)
    unchanged_hash = identity.content_hash(text.encode("utf-8"))
    ledger = {"a.md": _row(operation="update", status="in_flight", synced_hash=unchanged_hash)}
    rows = lookup(str(tmp_path), ledger, paths=["a.md"], want_body=True)
    assert rows[0]["stale"] is True
    assert rows[0]["content_stale"] is True
    assert rows[0]["current_hash"] == unchanged_hash  # content itself hasn't drifted...
    # ...but content_stale is still True because it's a sync-state stale marker.


def test_body_current_hash_differs_from_synced_hash_when_content_drifted(tmp_path):
    (tmp_path / "a.md").write_text("new content")
    row = _row(status="synced", synced_hash="deadbeef" * 4)
    b = body(str(tmp_path), "a.md", row)
    assert b.current_hash != b.synced_hash
    assert b.content_stale is True


# ---------------------------------------------------------------------------
# Privacy negative test: a seeded secret must never survive into body output.
# ---------------------------------------------------------------------------


def test_body_redacts_seeded_secret(tmp_path):
    secret = "api_key=abcdefghijklmnopqrstuvwxyz0123456789"
    (tmp_path / "leaky.md").write_text(f"---\ntitle: t\n---\nnormal text\n{secret}\nmore text")
    ledger = {"leaky.md": _row()}
    rows = lookup(str(tmp_path), ledger, paths=["leaky.md"], want_body=True)
    content = rows[0]["content"]
    assert secret not in content
    assert "[REDACTED_SECRET]" in content
