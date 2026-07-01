# tests/test_cass_corpus_atomic_write.py
import os
from cass_corpus import export


def test_atomic_write_content_exact(tmp_path):
    p = str(tmp_path / "t.md")
    export._atomic_write(p, "hello\nworld\n")
    assert open(p, encoding="utf-8").read() == "hello\nworld\n"


def test_atomic_write_no_tmp_left(tmp_path):
    p = str(tmp_path / "t.md")
    export._atomic_write(p, "x")
    leftovers = [f for f in os.listdir(tmp_path) if ".tmp." in f]
    assert leftovers == []


def test_atomic_write_overwrites(tmp_path):
    p = str(tmp_path / "t.md")
    export._atomic_write(p, "old")
    export._atomic_write(p, "new")
    assert open(p, encoding="utf-8").read() == "new"
