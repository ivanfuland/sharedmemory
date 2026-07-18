"""Step 3d / Task 11 reconciliation: StoreReader unit tests against small
synthetic on-disk fixtures matching the REAL EverOS shapes (real sqlite path
`.index/sqlite/system.db`, real columns, a real LanceDB table instead of a
JSON sidecar, real `{index}_{slug}.md` topic filenames). See
docs/projects/notes-memory/.../task-11-report.md "Shape validation" for the
live-run + source-read evidence these shapes come from.
"""
import sqlite3

import lancedb

from notes_adapter.probe.driver import StoreReader, parse_source_token

from _store_fixture import write_index_only, write_three_store_fixture


def _mk_system_db(root):
    sqlite_dir = root / ".index" / "sqlite"
    sqlite_dir.mkdir(parents=True)
    conn = sqlite3.connect(sqlite_dir / "system.db")
    conn.execute(
        "CREATE TABLE knowledge_documents (doc_id TEXT PRIMARY KEY, source_name TEXT, md_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE knowledge_topics (node_id TEXT, doc_id TEXT, summary TEXT, topic_name TEXT, "
        "topic_path TEXT, category_id TEXT, depth INT, content TEXT)"
    )
    conn.commit()
    conn.close()
    return sqlite3.connect(sqlite_dir / "system.db")


def test_sqlite_topics_and_document_row(tmp_path):
    conn = _mk_system_db(tmp_path)
    conn.execute(
        "INSERT INTO knowledge_documents VALUES ('doc1','nabc-g0.md','documents/doc1/index.md')"
    )
    conn.execute(
        "INSERT INTO knowledge_topics VALUES ('doc1_1','doc1','s','tn','tp','c',1,'body')"
    )
    conn.commit()
    conn.close()

    sr = StoreReader(str(tmp_path))
    topics = sr.sqlite_topics("doc1")
    assert len(topics) == 1 and topics[0].node_id == "doc1_1" and topics[0].content == "body"
    assert topics[0].content_sha256 == ""  # not a real column -- placeholder, see driver.py

    row = sr.document_row("doc1")
    assert row["source_name"] == "nabc-g0.md" and row["md_path"] == "documents/doc1/index.md"

    by_token = sr.document_row_by_token("nabc-g0.md")
    assert by_token["doc_id"] == "doc1"

    assert sr.sqlite_topics("missing") == []
    assert sr.document_row("missing") is None


def test_sqlite_topics_no_db_file_returns_empty(tmp_path):
    sr = StoreReader(str(tmp_path))
    assert sr.sqlite_topics("doc1") == []
    assert sr.document_row("doc1") is None


def test_sqlite_topics_wrong_schema_raises_not_silent_empty(tmp_path):
    """Task 11 finding #2 / concern #5 (fail-loud contract): once the path
    is correct, a missing column against an EXISTING db must raise, not
    silently return an empty result the way the old wrong-path bug did."""
    sqlite_dir = tmp_path / ".index" / "sqlite"
    sqlite_dir.mkdir(parents=True)
    conn = sqlite3.connect(sqlite_dir / "system.db")
    conn.execute("CREATE TABLE knowledge_topics (node_id TEXT, doc_id TEXT)")  # missing columns
    conn.commit()
    conn.close()

    sr = StoreReader(str(tmp_path))
    try:
        sr.sqlite_topics("doc1")
        assert False, "expected sqlite3.OperationalError, got a silent result instead"
    except sqlite3.OperationalError:
        pass


def test_lance_digests_real_lancedb_table(tmp_path):
    lancedb_dir = tmp_path / ".index" / "lancedb"
    lancedb_dir.mkdir(parents=True)
    db = lancedb.connect(str(lancedb_dir))
    db.create_table(
        "knowledge_topic",
        data=[
            {"id": "doc1_1", "doc_id": "doc1", "content_sha256": "digestvalue"},
            {"id": "doc2_1", "doc_id": "doc2", "content_sha256": "other-digest"},
        ],
    )
    sr = StoreReader(str(tmp_path))
    assert sr.lance_digests("doc1") == {"doc1_1": "digestvalue"}
    assert sr.lance_digests("no-such-doc") == {}


def test_lance_digests_no_lancedb_dir_returns_empty(tmp_path):
    sr = StoreReader(str(tmp_path))
    assert sr.lance_digests("doc1") == {}


def test_lance_digests_missing_table_raises_not_silent_empty(tmp_path):
    """Fail-loud contract: the `.index/lancedb/` directory existing but the
    `knowledge_topic` table missing/misnamed is a real bug, not a timing
    window -- must raise, not silently return {}."""
    lancedb_dir = tmp_path / ".index" / "lancedb"
    lancedb_dir.mkdir(parents=True)
    db = lancedb.connect(str(lancedb_dir))
    db.create_table("some_other_table", data=[{"id": "x"}])

    sr = StoreReader(str(tmp_path))
    try:
        sr.lance_digests("doc1")
        assert False, "expected a raise on missing 'knowledge_topic' table"
    except Exception as e:
        assert "knowledge_topic" in str(e) or "not found" in str(e).lower()


def test_topic_markdowns_reads_dir_keys_by_real_node_id(tmp_path):
    doc_dir = write_three_store_fixture(
        tmp_path, "doc1", "nabc-g0.md", 1, "s", "tn", "tp", "Others", 1, "body",
    )
    sr = StoreReader(str(tmp_path))
    md = sr.topic_markdowns(doc_dir, "doc1")
    assert set(md) == {"doc1_1"}  # real node_id = {doc_id}_{topic_index}, not the filename stem
    assert "body" in md["doc1_1"]


def test_topic_markdowns_skips_files_without_leading_index(tmp_path):
    ddir = tmp_path / "documents" / "doc1"
    ddir.mkdir(parents=True)
    (ddir / "index.md").write_text("---\nsource_token: nabc-g0.md\n---\n")
    (ddir / "not-a-topic-file.md").write_text("stray file, no leading int_ prefix")
    (ddir / "1_real_topic.md").write_text("---\nsummary: s\n---\nbody")
    sr = StoreReader(str(tmp_path))
    md = sr.topic_markdowns("documents/doc1", "doc1")
    assert set(md) == {"doc1_1"}


def test_topic_markdowns_missing_dir_returns_empty(tmp_path):
    sr = StoreReader(str(tmp_path))
    assert sr.topic_markdowns("documents/nope", "doc1") == {}


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


def test_cascade_pending_counts_pending_and_processing(tmp_path):
    sqlite_dir = tmp_path / ".index" / "sqlite"
    sqlite_dir.mkdir(parents=True)
    conn = sqlite3.connect(sqlite_dir / "system.db")
    conn.execute(
        "CREATE TABLE md_change_state (md_path TEXT, status TEXT)"
    )
    conn.execute("INSERT INTO md_change_state VALUES ('a.md', 'pending')")
    conn.execute("INSERT INTO md_change_state VALUES ('b.md', 'done')")
    conn.execute("INSERT INTO md_change_state VALUES ('c.md', 'processing')")
    conn.execute("INSERT INTO md_change_state VALUES ('d.md', 'failed')")
    conn.commit()
    conn.close()
    sr = StoreReader(str(tmp_path))
    assert sr.cascade_pending() == 2  # 'a.md' pending + 'c.md' processing


def test_cascade_pending_no_db_is_zero(tmp_path):
    sr = StoreReader(str(tmp_path))
    assert sr.cascade_pending() == 0


def test_parse_source_token():
    assert parse_source_token("nabc12345-g7.md") == ("abc12345", 7)
    assert parse_source_token("not-a-token.md") is None
    assert parse_source_token("random string") is None


def test_write_index_only_helper_smoke(tmp_path):
    """Sanity check on the shared fixture helper itself (used across
    test_accept_adoption.py / test_repair.py too)."""
    doc_dir = write_index_only(tmp_path, "doc-x", "ntoken-g0.md")
    sr = StoreReader(str(tmp_path))
    row = sr.document_row("doc-x")
    assert row["source_name"] == "ntoken-g0.md"
    assert row["md_path"] == f"{doc_dir}/index.md"
