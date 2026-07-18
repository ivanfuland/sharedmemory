"""Shared real-shaped on-disk EverOS store fixture builder, used by
test_store_reader.py / test_accept_adoption.py / test_repair.py.

Builds the REAL schema/paths Task 11 mapped against a live EverOS instance
(docs/projects/notes-memory/.../task-11-report.md "Shape validation"):
  <root>/.index/sqlite/system.db  - knowledge_documents(doc_id, source_name,
                                     md_path), knowledge_topics(node_id,
                                     doc_id, summary, topic_name, topic_path,
                                     category_id, depth, content) -- real
                                     column names, no source_token/doc_dir/
                                     content_sha256 columns
  <root>/.index/lancedb/          - real LanceDB dataset, table
                                     `knowledge_topic` (id/doc_id/
                                     content_sha256 columns used by
                                     StoreReader; no vector column needed
                                     since lance_digests() never selects it)
  <root>/documents/<doc_id>/index.md
  <root>/documents/<doc_id>/<topic_index>_<slug>.md  - real node_id is
                                     f"{doc_id}_{topic_index}", NOT the
                                     filename stem

`documents/<doc_id>` (rather than the real
`<app>/<project>/knowledge/<category>/<slug>_<doc_id>` nesting) is kept as
the doc_dir convention here for test simplicity -- StoreReader accepts any
root-relative or absolute doc_dir string, and these tests only exercise
StoreReader's read logic, not EverOS's write-side directory layout.
"""
from __future__ import annotations

import sqlite3

import lancedb

from notes_adapter.digest import canonical_digest


def write_topics_fixture(store_root, doc_id, source_token, topics: list[dict]) -> str:
    """`topics`: list of dicts with keys topic_index(int), summary,
    topic_name, topic_path, category_id, depth, content, and optional slug
    (defaults to topic_name). Returns the doc_dir (root-relative)."""
    doc_dir = f"documents/{doc_id}"

    sqlite_dir = store_root / ".index" / "sqlite"
    sqlite_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sqlite_dir / "system.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_documents "
        "(doc_id TEXT PRIMARY KEY, source_name TEXT, md_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_topics "
        "(node_id TEXT, doc_id TEXT, summary TEXT, topic_name TEXT, "
        "topic_path TEXT, category_id TEXT, depth INT, content TEXT)"
    )
    # A real, fully-bootstrapped system.db always has this table too (it's
    # part of the base schema, not created lazily) -- included here, empty,
    # so cascade_pending()'s fail-loud "missing table = bug" contract isn't
    # tripped by these tests' incomplete-by-omission fixture db.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS md_change_state (md_path TEXT PRIMARY KEY, status TEXT)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO knowledge_documents VALUES (?,?,?)",
        (doc_id, source_token, f"{doc_dir}/index.md"),
    )

    ddir = store_root / doc_dir
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "index.md").write_text(f"---\nsource_token: {source_token}\n---\n")

    lance_rows = []
    for t in topics:
        node_id = f"{doc_id}_{t['topic_index']}"
        conn.execute(
            "INSERT INTO knowledge_topics VALUES (?,?,?,?,?,?,?,?)",
            (
                node_id,
                doc_id,
                t["summary"],
                t["topic_name"],
                t["topic_path"],
                t["category_id"],
                t["depth"],
                t["content"],
            ),
        )
        digest = canonical_digest(
            t["summary"], t["topic_name"], t["topic_path"], t["category_id"],
            t["depth"], t["content"],
        )
        lance_rows.append({"id": node_id, "doc_id": doc_id, "content_sha256": digest})
        slug = t.get("slug") or t["topic_name"] or "t"
        md = (
            f"---\nsummary: {t['summary']}\ntopic_name: {t['topic_name']}\n"
            f"topic_path: {t['topic_path']}\ncategory_id: {t['category_id']}\n"
            f"depth: {t['depth']}\n---\n{t['content']}"
        )
        (ddir / f"{t['topic_index']}_{slug}.md").write_text(md)

    conn.commit()
    conn.close()

    lancedb_dir = store_root / ".index" / "lancedb"
    lancedb_dir.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(lancedb_dir))
    if "knowledge_topic" in db.list_tables().tables:
        db.open_table("knowledge_topic").add(lance_rows)
    else:
        db.create_table("knowledge_topic", data=lance_rows)

    return doc_dir


def write_three_store_fixture(
    store_root, doc_id, source_token, topic_index, summary, topic_name,
    topic_path, category_id, depth, content, slug=None,
):
    """Single-topic convenience wrapper (the common case in existing
    tests)."""
    return write_topics_fixture(
        store_root,
        doc_id,
        source_token,
        [
            dict(
                topic_index=topic_index,
                summary=summary,
                topic_name=topic_name,
                topic_path=topic_path,
                category_id=category_id,
                depth=depth,
                content=content,
                slug=slug,
            )
        ],
    )


def write_index_only(store_root, doc_id, source_token) -> str:
    """Document row + index.md only, no topics -- for adoption tests that
    only need iter_document_dirs() (filesystem-based, unaffected by the
    SQLite/Lance shape fixes) to see the doc exists."""
    doc_dir = f"documents/{doc_id}"
    sqlite_dir = store_root / ".index" / "sqlite"
    sqlite_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sqlite_dir / "system.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_documents "
        "(doc_id TEXT PRIMARY KEY, source_name TEXT, md_path TEXT)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO knowledge_documents VALUES (?,?,?)",
        (doc_id, source_token, f"{doc_dir}/index.md"),
    )
    conn.commit()
    conn.close()

    ddir = store_root / doc_dir
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "index.md").write_text(f"---\nsource_token: {source_token}\n---\n")
    return doc_dir
