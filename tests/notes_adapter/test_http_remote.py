"""Step 3c / Task 11 reconciliation: HttpRemote contract tests against
hand-written synthetic fixtures matching the REAL EverOS response shapes
confirmed live in Task 11 (envelope {"request_id":.., "data":{..}};
POST's doc dir field is `md_path`, not `doc_dir`; DELETE's count field is
`deleted_topics`, may be a bare 204; list_docs's array field is
`documents`). See docs/projects/notes-memory/.../task-11-report.md "Shape
validation" #1/#5. No network — `_request` is monkeypatched so these never
touch a real instance. Redacted/synthetic only, safe for the PUBLIC repo.

No search() tests here: HttpRemote.search() was removed (Task 11 finding —
wrong HTTP method/param encoding, and zero real callers; the correct,
already-tested retrieval path is notes_adapter.search_core.search()).
"""
import urllib.error

import pytest

from notes_adapter.frontmatter import PreparedDocument
from notes_adapter.probe.driver import HttpRemote, HttpRemoteError, _build_multipart


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


# ---------------------------------------------------------------------------
# _build_multipart() (Minor, T8 review: previously zero coverage)
# ---------------------------------------------------------------------------


def test_build_multipart_encodes_title_category_id_and_filename_parts():
    boundary = "testboundary123"
    doc = PreparedDocument(
        source_token="nabc12345-g0.md",
        title="My Title",
        category_id="Others",
        redacted=b"---\ntitle: t\n---\nbody text",
        raw_hash="rawhash",
        redacted_hash="redhash",
        fingerprint="fp",
    )
    body = _build_multipart(boundary, doc)

    assert body.startswith(f"--{boundary}\r\n".encode("utf-8"))
    assert body.endswith(f"--{boundary}--\r\n".encode("utf-8"))

    # Split into the delimited parts, dropping the empty preamble chunk
    # (before the first delimiter) and the "--\r\n" closing-delimiter
    # suffix, leaving exactly the three form-data field bodies.
    raw_parts = body.split(f"--{boundary}".encode("utf-8"))
    parts = [p.lstrip(b"\r\n") for p in raw_parts if p not in (b"", b"--\r\n")]
    assert len(parts) == 3  # title, category_id, file

    title_part, category_part, file_part = parts

    assert b'Content-Disposition: form-data; name="title"' in title_part
    assert title_part.rstrip(b"\r\n").endswith(b"My Title")

    assert b'Content-Disposition: form-data; name="category_id"' in category_part
    assert category_part.rstrip(b"\r\n").endswith(b"Others")

    assert (
        b'Content-Disposition: form-data; name="file"; filename="nabc12345-g0.md"'
        in file_part
    )
    assert b"Content-Type: text/markdown" in file_part
    assert file_part.rstrip(b"\r\n").endswith(doc.redacted)


# ---------------------------------------------------------------------------
# post() — real DocumentCreateResponse shape (Task 11 finding #1)
# ---------------------------------------------------------------------------


def test_post_parses_envelope_and_md_path_as_doc_dir(monkeypatch):
    """Real response has no `doc_dir` key -- the absolute doc directory is
    under the confusingly-named `md_path` key (service/knowledge.py assigns
    KnowledgeWriter.write()'s doc_dir return value to a var it calls
    md_path)."""
    remote = HttpRemote("http://127.0.0.1:8031")
    captured = {}

    def fake_request(self, method, path, data=None, headers=None):
        captured["method"], captured["path"] = method, path
        return {
            "request_id": "req-synthetic-1",
            "data": {
                "doc_id": "doc-synthetic-1",
                "category_id": "Others",
                "topic_count": 3,
                "source_name": "nabc12345-g0.md",
                "md_path": "/data/doc-synthetic-1",
                "original_file_path": "/data/doc-synthetic-1/_original/nabc12345-g0.md",
            },
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


def test_post_missing_md_path_raises(monkeypatch):
    """Old field name (`doc_dir`) present but not the real one (`md_path`)
    must still be treated as missing -- this is exactly the Task 11 live
    failure mode before the fix."""
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote,
        "_request",
        lambda self, m, p, data=None, headers=None: {
            "request_id": "r",
            "data": {"doc_id": "doc-1", "doc_dir": "/data/doc-1"},  # old wrong key, no md_path
        },
    )
    with pytest.raises(ValueError):
        remote.post(_doc())


# ---------------------------------------------------------------------------
# _request() — non-2xx handling (Task 11 finding: real 503 hit live)
# ---------------------------------------------------------------------------


def test_request_raises_http_remote_error_on_non_2xx(monkeypatch):
    import io

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", hdrs=None,
            fp=io.BytesIO(b'{"detail":"capability_error"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    remote = HttpRemote("http://127.0.0.1:8031")
    with pytest.raises(HttpRemoteError) as exc_info:
        remote._request("POST", "/api/v1/knowledge/documents", data=b"x")
    assert exc_info.value.status == 503
    assert "capability_error" in exc_info.value.body


def test_request_empty_body_returns_none(monkeypatch):
    """Real DELETE returns bare 204 No Content -- _request must not crash
    trying to json.loads an empty body."""
    import io

    class _FakeResp:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResp())
    remote = HttpRemote("http://127.0.0.1:8031")
    assert remote._request("DELETE", "/api/v1/knowledge/documents/doc-1") is None


# ---------------------------------------------------------------------------
# delete() — real deleted_topics field + bare 204 (Task 11 finding #5)
# ---------------------------------------------------------------------------


def test_delete_parses_deleted_topics_count(monkeypatch):
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote,
        "_request",
        lambda self, m, p, data=None, headers=None: {
            "request_id": "r",
            "data": {"doc_id": "doc-synthetic-1", "deleted_topics": 3},
        },
    )
    assert remote.delete("doc-synthetic-1") == 3


def test_delete_treats_empty_204_body_as_zero(monkeypatch):
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote, "_request", lambda self, m, p, data=None, headers=None: None
    )
    assert remote.delete("doc-1") == 0


# ---------------------------------------------------------------------------
# list_docs() — real `documents` array field (Task 11 finding #5)
# ---------------------------------------------------------------------------


def test_list_docs_parses_documents_array(monkeypatch):
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote,
        "_request",
        lambda self, m, p, data=None, headers=None: {
            "request_id": "r",
            "data": {"documents": [{"doc_id": "d1"}, {"doc_id": "d2"}]},
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


def test_malformed_envelope_missing_data_key_raises(monkeypatch):
    """Distinct from the bare-204/empty-body short circuit (payload is None,
    handled without touching the envelope at all) -- this is a non-empty but
    malformed JSON body, which must still hit _unwrap_envelope's check."""
    remote = HttpRemote("http://127.0.0.1:8031")
    monkeypatch.setattr(
        HttpRemote, "_request", lambda self, m, p, data=None, headers=None: {"request_id": "r"}
    )
    with pytest.raises(ValueError):
        remote.delete("doc-1")
