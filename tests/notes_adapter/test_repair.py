"""Step 3f: repair() — four mechanical preconditions (spec §5.5), the fence
re-check, and the injectable `repair_runner` seam. The real `force_enqueue`
subprocess + real Lance deletion under an isolated env are validated in
Task 13's fault injection; here everything live is a stub the test controls.
"""
import hashlib
import json
import sqlite3

import pytest

from notes_adapter import identity
from notes_adapter.digest import canonical_digest
from notes_adapter.probe.driver import Driver, FakeRemote, RepairPreconditionError, ScanResult


def mk_scan(paths, root):
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
    def post(self, doc):
        self._seq += 1
        doc_id = f"fake-doc-{self._seq}"
        doc_dir = f"documents/{doc_id}"
        self.docs[doc_id] = {"source_token": doc.source_token, "doc_dir": doc_dir, "topic_count": 1}
        return doc_id, doc_dir, 1


def _write_three_store_fixture(store_root, doc_id, source_token, node_id, content):
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
        (node_id, doc_id, "s", "tn", "tp", "Others", 1, content, hashlib.sha256(content.encode()).hexdigest()),
    )
    conn.commit()
    conn.close()

    (store_root / "lance").mkdir(exist_ok=True)
    digest = canonical_digest("s", "tn", "tp", "Others", 1, content)
    (store_root / "lance" / f"{doc_id}.json").write_text(json.dumps({node_id: digest}))

    ddir = store_root / doc_dir
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "index.md").write_text(f"---\nsource_token: {source_token}\n---\n")
    (ddir / f"{node_id}.md").write_text(
        f"---\nsummary: s\ntopic_name: tn\ntopic_path: tp\ncategory_id: Others\ndepth: 1\n---\n{content}"
    )
    return doc_dir


def _synced_driver(tmp_path, content="hello world"):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text(f"---\ntitle: t\n---\n{content}")
    d = Driver(":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store))
    d.scan_result(mk_scan(["a.md"], vault))
    d.run_once()
    row = d.row("a.md")
    doc_id = row["staging_doc_id"]
    token = identity.source_token("a.md", row["generation"])
    _write_three_store_fixture(store, doc_id, token, "n1", content)
    d.run_once()
    assert d.row("a.md")["status"] == "synced"
    return d, store


def test_repair_rejects_fewer_than_two_observations(tmp_path):
    d, store = _synced_driver(tmp_path)
    with pytest.raises(RepairPreconditionError, match="fewer than two"):
        d.repair("a.md", everos_root=str(tmp_path / "everos-root"), observations=[(0.0, "fpA")])


def test_repair_rejects_no_observations(tmp_path):
    d, store = _synced_driver(tmp_path)
    with pytest.raises(RepairPreconditionError, match="fewer than two"):
        d.repair("a.md", everos_root=str(tmp_path / "everos-root"))


def test_repair_rejects_drift_fingerprint_mismatch(tmp_path):
    d, store = _synced_driver(tmp_path)
    with pytest.raises(RepairPreconditionError, match="not stable"):
        d.repair(
            "a.md",
            everos_root=str(tmp_path / "everos-root"),
            observations=[(0.0, "fpA"), (10.0, "fpB")],
        )


def test_repair_rejects_spacing_too_short(tmp_path):
    d, store = _synced_driver(tmp_path)
    with pytest.raises(RepairPreconditionError, match="spaced <"):
        d.repair(
            "a.md",
            everos_root=str(tmp_path / "everos-root"),
            observations=[(0.0, "fpA"), (1.0, "fpA")],
            min_interval=10.0,
        )


def test_repair_rejects_when_cascade_queue_not_empty(tmp_path):
    d, store = _synced_driver(tmp_path)
    conn = sqlite3.connect(store / "md_change_state.db")
    conn.execute("CREATE TABLE md_change_state (path TEXT, processed INT)")
    conn.execute("INSERT INTO md_change_state VALUES ('other.md', 0)")
    conn.commit()
    conn.close()
    with pytest.raises(RepairPreconditionError, match="cascade queue not empty"):
        d.repair(
            "a.md",
            everos_root=str(tmp_path / "everos-root"),
            observations=[(0.0, "fpA"), (10.0, "fpA")],
        )


def test_repair_no_ledger_row_raises(tmp_path):
    d, store = _synced_driver(tmp_path)
    with pytest.raises(RepairPreconditionError, match="no ledger row"):
        d.repair(
            "no-such-path.md",
            everos_root=str(tmp_path / "everos-root"),
            observations=[(0.0, "fpA"), (10.0, "fpA")],
        )


def test_repair_without_repair_runner_raises(tmp_path):
    d, store = _synced_driver(tmp_path)
    with pytest.raises(RepairPreconditionError, match="no repair_runner injected"):
        d.repair(
            "a.md",
            everos_root=str(tmp_path / "everos-root"),
            observations=[(0.0, "fpA"), (10.0, "fpA")],
        )


def test_repair_happy_path_calls_injected_runner_and_reverifies(tmp_path):
    calls = []

    def stub_runner(path, node_ids, everos_root):
        calls.append((path, node_ids, everos_root))
        # canonical repair converges: store fixture already matches, so the
        # post-repair reverify (which re-reads the same fixture) succeeds.

    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("---\ntitle: t\n---\nhello world")
    d = Driver(
        ":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store), repair_runner=stub_runner
    )
    d.scan_result(mk_scan(["a.md"], vault))
    d.run_once()
    row = d.row("a.md")
    doc_id = row["staging_doc_id"]
    token = identity.source_token("a.md", row["generation"])
    _write_three_store_fixture(store, doc_id, token, "n1", "hello world")
    d.run_once()
    assert d.row("a.md")["status"] == "synced"

    everos_root = str(tmp_path / "everos-root")
    result = d.repair(
        "a.md", everos_root=everos_root, observations=[(0.0, "fpA"), (10.0, "fpA")]
    )
    assert result == {"path": "a.md", "repaired": True}
    assert len(calls) == 1
    called_path, called_node_ids, called_root = calls[0]
    assert called_path == "a.md"
    assert called_node_ids == ["n1"]
    assert called_root == everos_root


def test_repair_runner_failure_reports_escalate_delete_recreate(tmp_path):
    def stub_runner(path, node_ids, everos_root):
        pass  # simulate a repair that doesn't converge: don't fix the fixture

    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (vault / "a.md").write_text("---\ntitle: t\n---\nhello world")
    d = Driver(
        ":memory:", _RelDirFakeRemote(), str(vault), memory_root=str(store), repair_runner=stub_runner
    )
    d.scan_result(mk_scan(["a.md"], vault))
    d.run_once()
    row = d.row("a.md")
    doc_id = row["staging_doc_id"]
    token = identity.source_token("a.md", row["generation"])
    _write_three_store_fixture(store, doc_id, token, "n1", "hello world")
    d.run_once()

    # break the store AFTER sync so repair's reverify fails (simulating a
    # repair that didn't converge)
    (store / "lance" / f"{doc_id}.json").write_text(json.dumps({"n1": "wrong-digest"}))

    result = d.repair(
        "a.md",
        everos_root=str(tmp_path / "everos-root"),
        observations=[(0.0, "fpA"), (10.0, "fpA")],
    )
    assert result["repaired"] is False
    assert result["escalate"] == "delete+recreate"


def test_repair_fence_rejects_when_state_changed_since_precondition_check(tmp_path, monkeypatch):
    d, store = _synced_driver(tmp_path)

    orig_get = d.ledger.get
    calls = {"n": 0}

    def flaky_get(path):
        calls["n"] += 1
        row = orig_get(path)
        if calls["n"] == 2:
            # simulate the row moving between the precondition check (1st
            # get) and the fence re-check (2nd get)
            d.ledger._conn.execute(
                "UPDATE notes_ledger SET active_doc_id='someone-else' WHERE path=?", (path,)
            )
            d.ledger._conn.commit()
            return orig_get(path)
        return row

    monkeypatch.setattr(d.ledger, "get", flaky_get)
    with pytest.raises(RepairPreconditionError, match="fenced state changed"):
        d.repair(
            "a.md",
            everos_root=str(tmp_path / "everos-root"),
            observations=[(0.0, "fpA"), (10.0, "fpA")],
        )
