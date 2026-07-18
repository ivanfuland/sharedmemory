"""Task 10 Step 1-2: search_core — fold_and_filter (frozen pure-function
contract) + search() (HTTP shell, fake http_post + in-memory ledger, no
network). Real-instance contract validation is a later task, mirroring the
HttpRemote / test_http_remote.py split.
"""
from notes_adapter.search_core import Eligibility, eligibility, fold_and_filter, search


# ---------------------------------------------------------------------------
# Step 1: fold_and_filter — verbatim from the task brief.
# ---------------------------------------------------------------------------


def test_fold_filter_partial():
    raw = [
        {"doc_id": "d1", "score": 0.9},
        {"doc_id": "d1", "score": 0.8},
        {"doc_id": "dX", "score": 0.7},
        {"doc_id": "d2", "score": 0.6},
    ]
    ledger = {
        "d1": {"path": "a.md", "admitted": True},
        "d2": {"path": "b.md", "admitted": True},
        "dX": {"path": "x.md", "admitted": False},
    }
    hits, partial = fold_and_filter(raw, ledger, top_k=3)
    assert [h["path"] for h in hits] == ["a.md", "b.md"] and partial is True


def test_fold_filter_best_score_is_max_across_folded_cards():
    raw = [
        {"doc_id": "d1", "score": 0.4},
        {"doc_id": "d1", "score": 0.95},
        {"doc_id": "d1", "score": 0.6},
    ]
    ledger = {"d1": {"path": "a.md", "admitted": True}}
    hits, partial = fold_and_filter(raw, ledger, top_k=5)
    assert hits[0]["best_score"] == 0.95
    assert len(hits[0]["cards"]) == 3
    assert partial is True  # 1 hit < top_k=5


def test_fold_filter_not_partial_when_full():
    raw = [{"doc_id": f"d{i}", "score": 1.0 - i * 0.1} for i in range(5)]
    ledger = {f"d{i}": {"path": f"{i}.md", "admitted": True} for i in range(5)}
    hits, partial = fold_and_filter(raw, ledger, top_k=5)
    assert len(hits) == 5 and partial is False


def test_fold_filter_doc_id_missing_from_ledger_is_excluded():
    raw = [{"doc_id": "unknown", "score": 0.9}, {"doc_id": "d1", "score": 0.5}]
    ledger = {"d1": {"path": "a.md", "admitted": True}}
    hits, partial = fold_and_filter(raw, ledger, top_k=2)
    assert [h["path"] for h in hits] == ["a.md"]


# ---------------------------------------------------------------------------
# eligibility() — spec §8 matrix, one assertion group per row (shared with
# lookup_core; lookup_core's own test suite exercises the same function
# against its lookup-specific axes).
# ---------------------------------------------------------------------------


def test_eligibility_synced():
    e = eligibility("none", "doc-1", True, "synced")
    assert e == Eligibility(True, True, True, True, False)


def test_eligibility_update_inflight_with_old_active_is_stale_visible():
    e = eligibility("update", "doc-1", True, "in_flight")
    assert e.search_visible and e.stale and e.body_allowed


def test_eligibility_first_create_no_active_excluded_but_debuggable():
    e = eligibility("create", None, True, "pending")
    assert not e.search_visible and not e.lookup_default_visible
    assert e.lookup_include_status_visible and not e.body_allowed


def test_eligibility_delete_intent_confirmed_hidden():
    e = eligibility("delete", "doc-1", True, "in_flight")
    assert not e.search_visible and not e.body_allowed
    assert e.lookup_include_status_visible


def test_eligibility_not_admitted_overrides_everything():
    e = eligibility("none", "doc-1", False, "synced")
    assert e == Eligibility(False, False, False, False, False)


# ---------------------------------------------------------------------------
# Step 2: search() — HTTP shell against an injected fake + in-memory ledger.
# ---------------------------------------------------------------------------


def _envelope(hits):
    return {"request_id": "req-1", "data": {"hits": hits}}


def test_search_hit_returns_folded_hit_joined_to_ledger():
    captured = {}

    def fake_post(base_url, path, body):
        captured["base_url"], captured["path"], captured["body"] = base_url, path, body
        return _envelope(
            [
                {"score": 0.9, "document": {"doc_id": "doc-1", "title": "WSL 重装"}},
            ]
        )

    ledger = {
        "doc-1": {
            "path": "865-Remote 工作流/04-终端工具链/01-WSL/001-重装WSL【聚合】.md",
            "admitted": True,
            "active_doc_id": "doc-1",
            "operation": "none",
            "status": "synced",
            "synced_frontmatter": {"tags": [], "date": "2025-08-05"},
        }
    }
    hits = search("http://127.0.0.1:9", ledger, "WSL 重装", top_k=5, overfetch_mult=4, http_post=fake_post)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.doc_id == "doc-1"
    assert hit.path.endswith("001-重装WSL【聚合】.md")
    assert hit.best_score == 0.9
    assert hit.stale is False
    assert hit.metadata_stale is False

    # request-body contract: top_k=min(top_k*mult,100), no `limit` field.
    assert captured["path"] == "/api/v1/knowledge/search"
    assert captured["body"]["top_k"] == 20
    assert "limit" not in captured["body"]


def test_search_excludes_not_admitted_and_ineligible_docs():
    def fake_post(base_url, path, body):
        return _envelope(
            [
                {"score": 0.9, "document": {"doc_id": "doc-excluded"}},
                {"score": 0.5, "document": {"doc_id": "doc-dead"}},
                {"score": 0.4, "document": {"doc_id": "doc-ok"}},
            ]
        )

    ledger = {
        "doc-excluded": {
            "path": "x.md",
            "admitted": False,  # 撤出白名单
            "active_doc_id": "doc-excluded",
            "operation": "none",
            "status": "synced",
            "synced_frontmatter": {},
        },
        "doc-dead": {
            "path": "y.md",
            "admitted": True,
            "active_doc_id": None,  # dead: no active
            "operation": "none",
            "status": "dead",
            "synced_frontmatter": {},
        },
        "doc-ok": {
            "path": "z.md",
            "admitted": True,
            "active_doc_id": "doc-ok",
            "operation": "none",
            "status": "synced",
            "synced_frontmatter": {},
        },
    }
    hits = search("http://127.0.0.1:9", ledger, "q", http_post=fake_post)
    assert [h.path for h in hits] == ["z.md"]


def test_search_metadata_stale_when_synced_frontmatter_differs_from_latest():
    def fake_post(base_url, path, body):
        return _envelope([{"score": 0.9, "document": {"doc_id": "doc-1"}}])

    ledger = {
        "doc-1": {
            "path": "a.md",
            "admitted": True,
            "active_doc_id": "doc-1",
            "operation": "none",
            "status": "synced",
            "synced_frontmatter": {"tags": ["old"]},
            "frontmatter": {"tags": ["new"]},  # 最新扫描代已经变了
        }
    }
    hits = search("http://127.0.0.1:9", ledger, "q", http_post=fake_post)
    assert hits[0].metadata_stale is True
    assert hits[0].stale is False  # sync-state stale (§8) is a separate axis


def test_search_no_metadata_stale_when_no_newer_frontmatter_seen():
    def fake_post(base_url, path, body):
        return _envelope([{"score": 0.9, "document": {"doc_id": "doc-1"}}])

    ledger = {
        "doc-1": {
            "path": "a.md",
            "admitted": True,
            "active_doc_id": "doc-1",
            "operation": "none",
            "status": "synced",
            "synced_frontmatter": {"tags": ["x"]},
            "frontmatter": None,
        }
    }
    hits = search("http://127.0.0.1:9", ledger, "q", http_post=fake_post)
    assert hits[0].metadata_stale is False


def test_search_malformed_envelope_missing_data_raises():
    import pytest

    def fake_post(base_url, path, body):
        return {"request_id": "r"}

    with pytest.raises(ValueError):
        search("http://127.0.0.1:9", {}, "q", http_post=fake_post)


def test_search_hit_missing_nested_document_doc_id_raises():
    import pytest

    def fake_post(base_url, path, body):
        return _envelope([{"score": 0.9}])  # no "document" at all

    with pytest.raises(ValueError):
        search("http://127.0.0.1:9", {}, "q", http_post=fake_post)
