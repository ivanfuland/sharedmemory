"""Step 3d: StoreReader unit tests against small synthetic on-disk fixtures
(a temp SQLite with the expected columns, a temp markdown dir, a JSON Lance
sidecar). NOT a real EverOS store — real-store shape validation is Task 11.
"""
import json
import sqlite3

from notes_adapter.probe.driver import StoreReader, parse_source_token


def _mk_system_db(root):
    conn = sqlite3.connect(root / "system.db")
    conn.execute(
        "CREATE TABLE knowledge_documents (doc_id TEXT PRIMARY KEY, source_token TEXT, doc_dir TEXT)"
    )
    conn.execute(
        "CREATE TABLE knowledge_topics (node_id TEXT, doc_id TEXT, summary TEXT, topic_name TEXT, "
        "topic_path TEXT, category_id TEXT, depth INT, content TEXT, content_sha256 TEXT)"
    )
    conn.commit()
    conn.close()
    return sqlite3.connect(root / "system.db")


def test_sqlite_topics_and_document_row(tmp_path):
    conn = _mk_system_db(tmp_path)
    conn.execute(
        "INSERT INTO knowledge_documents VALUES ('doc1','nabc-g0.md','/data/doc1')"
    )
    conn.execute(
        "INSERT INTO knowledge_topics VALUES ('n1','doc1','s','tn','tp','c',1,'body','sha')"
    )
    conn.commit()
    conn.close()

    sr = StoreReader(str(tmp_path))
    topics = sr.sqlite_topics("doc1")
    assert len(topics) == 1 and topics[0].node_id == "n1" and topics[0].content == "body"

    row = sr.document_row("doc1")
    assert row["source_token"] == "nabc-g0.md" and row["doc_dir"] == "/data/doc1"

    by_token = sr.document_row_by_token("nabc-g0.md")
    assert by_token["doc_id"] == "doc1"

    assert sr.sqlite_topics("missing") == []
    assert sr.document_row("missing") is None


def test_sqlite_topics_no_db_file_returns_empty(tmp_path):
    sr = StoreReader(str(tmp_path))
    assert sr.sqlite_topics("doc1") == []
    assert sr.document_row("doc1") is None


def test_lance_digests_json_sidecar(tmp_path):
    (tmp_path / "lance").mkdir()
    (tmp_path / "lance" / "doc1.json").write_text(json.dumps({"n1": "digestvalue"}))
    sr = StoreReader(str(tmp_path))
    assert sr.lance_digests("doc1") == {"n1": "digestvalue"}
    assert sr.lance_digests("no-such-doc") == {}


def test_topic_markdowns_reads_dir_excludes_index(tmp_path):
    doc_dir = tmp_path / "documents" / "doc1"
    doc_dir.mkdir(parents=True)
    (doc_dir / "index.md").write_text("---\nsource_token: nabc-g0.md\n---\n")
    (doc_dir / "n1.md").write_text("---\nsummary: s\n---\nbody")
    sr = StoreReader(str(tmp_path))
    md = sr.topic_markdowns("documents/doc1")
    assert set(md) == {"n1"}
    assert "body" in md["n1"]


def test_topic_markdowns_missing_dir_returns_empty(tmp_path):
    sr = StoreReader(str(tmp_path))
    assert sr.topic_markdowns("documents/nope") == {}


def test_iter_document_dirs_markdown_first_enumeration(tmp_path):
    d1 = tmp_path / "documents" / "doc1"
    d1.mkdir(parents=True)
    (d1 / "index.md").write_text("---\nsource_token: nabc-g0.md\ntitle: t\n---\n")
    d2 = tmp_path / "documents" / "doc2"
    d2.mkdir(parents=True)
    (d2 / "index.md").write_text("---\nsource_token: ndef-g1.md\n---\n")
    sr = StoreReader(str(tmp_path))
    found = dict(sr.iter_document_dirs())
    assert set(found) == {"nabc-g0.md", "ndef-g1.md"}
    assert found["nabc-g0.md"].endswith("documents/doc1")


def test_iter_document_dirs_skips_dirs_without_index_or_token(tmp_path):
    """R9 nit5 coverage: a POST that landed markdown but crashed before the
    document row/index commit is a dir with no usable index.md yet -- must
    not be yielded as adoptable (nothing to key off), and must not crash."""
    incomplete = tmp_path / "documents" / "doc-crashed"
    incomplete.mkdir(parents=True)  # no index.md at all -- crashed mid-POST
    good = tmp_path / "documents" / "doc-good"
    good.mkdir(parents=True)
    (good / "index.md").write_text("---\nsource_token: nabc-g0.md\n---\n")
    no_token = tmp_path / "documents" / "doc-no-token"
    no_token.mkdir(parents=True)
    (no_token / "index.md").write_text("---\ntitle: whatever\n---\n")  # no source_token field

    sr = StoreReader(str(tmp_path))
    found = dict(sr.iter_document_dirs())
    assert set(found) == {"nabc-g0.md"}


def test_iter_document_dirs_no_documents_root(tmp_path):
    sr = StoreReader(str(tmp_path))
    assert list(sr.iter_document_dirs()) == []


def test_cascade_pending_counts_unprocessed(tmp_path):
    conn = sqlite3.connect(tmp_path / "md_change_state.db")
    conn.execute("CREATE TABLE md_change_state (path TEXT, processed INT)")
    conn.execute("INSERT INTO md_change_state VALUES ('a.md', 0)")
    conn.execute("INSERT INTO md_change_state VALUES ('b.md', 1)")
    conn.execute("INSERT INTO md_change_state VALUES ('c.md', 0)")
    conn.commit()
    conn.close()
    sr = StoreReader(str(tmp_path))
    assert sr.cascade_pending() == 2


def test_cascade_pending_no_db_is_zero(tmp_path):
    sr = StoreReader(str(tmp_path))
    assert sr.cascade_pending() == 0


def test_parse_source_token():
    assert parse_source_token("nabc12345-g7.md") == ("abc12345", 7)
    assert parse_source_token("not-a-token.md") is None
    assert parse_source_token("random string") is None
