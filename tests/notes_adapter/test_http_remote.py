"""Step 3c: HttpRemote contract tests against hand-written synthetic fixtures
matching the code-read EverOS response shapes given in the task brief
(envelope {"request_id":.., "data":{..}}; search hit nested under
hit["document"]["doc_id"]). No network — `_request` is monkeypatched so
these never touch a real instance. Redacted/synthetic only, safe for the
PUBLIC repo. Live contract validation against a real instance is Task 11.
"""
import pytest

from notes_adapter.frontmatter import PreparedDocument
from notes_adapter.probe.driver import HttpRemote


def _doc():
    return PreparedDocument(
        source_token="nabc12345-g0.md",
        title="t",
        category_id="Others",
        redacted=b"---\ntitle: t\n---\nbody",
        raw_hash="rawhash",
        redacted_hash="redhash",
        fingerprint="fp",
    )


def test_post_parses_envelope_and_nested_doc_fields(monkeypatch):
    remote = HttpRemote("http://127.0.0.1:8031")
    captured = {}

    def fake_request(self, method, path, data=None, headers=None):
        captured["method"], captured["path"] = method, path
        return {
            "request_id": "req-synthetic-1",
            "data": {"doc_id": "doc-synthetic-1", "doc_dir": "/data/doc-synthetic-1", "topic_count": 3},
        }

    monkeypatch.setattr(HttpRemote, "_request", fake_request)
    doc_id, doc_dir, topic_count = remote.post(_doc())
    assert (doc_id, doc_dir, topic_count) == ("doc-synthetic-1", "/data/doc-synthetic-1", 3)
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/knowledge/documents"


def test_post_missing_doc_id_raises(monkeypatch):
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote, "_request", lambda self, m, p, data=None, headers=None: {"request_id": "r", "data": {}}
    )
    with pytest.raises(ValueError):
        remote.post(_doc())


def test_delete_parses_deleted_count(monkeypatch):
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote,
        "_request",
        lambda self, m, p, data=None, headers=None: {"request_id": "r", "data": {"deleted": 1}},
    )
    assert remote.delete("doc-synthetic-1") == 1


def test_list_docs_parses_items_array(monkeypatch):
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote,
        "_request",
        lambda self, m, p, data=None, headers=None: {
            "request_id": "r",
            "data": {"items": [{"doc_id": "d1"}, {"doc_id": "d2"}]},
        },
    )
    items = remote.list_docs(page=1)
    assert [i["doc_id"] for i in items] == ["d1", "d2"]


def test_list_docs_malformed_response_raises(monkeypatch):
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote, "_request", lambda self, m, p, data=None, headers=None: {"request_id": "r", "data": {}}
    )
    with pytest.raises(ValueError):
        remote.list_docs()


def test_search_hit_document_doc_id_nested(monkeypatch):
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote,
        "_request",
        lambda self, m, p, data=None, headers=None: {
            "request_id": "r",
            "data": {
                "hits": [
                    {"score": 0.9, "document": {"doc_id": "doc-1", "title": "t"}},
                    {"score": 0.5, "document": {"doc_id": "doc-2", "title": "u"}},
                ]
            },
        },
    )
    hits = remote.search("query")
    assert [h["document"]["doc_id"] for h in hits] == ["doc-1", "doc-2"]


def test_search_hit_missing_document_doc_id_raises(monkeypatch):
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote,
        "_request",
        lambda self, m, p, data=None, headers=None: {
            "request_id": "r",
            "data": {"hits": [{"score": 0.9}]},  # missing "document" entirely
        },
    )
    with pytest.raises(ValueError):
        remote.search("query")


def test_malformed_envelope_missing_data_key_raises(monkeypatch):
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote, "_request", lambda self, m, p, data=None, headers=None: {"request_id": "r"}
    )
    with pytest.raises(ValueError):
        remote.delete("doc-1")
