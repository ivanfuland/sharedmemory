# tests/test_cass_corpus_state.py
import os
from cass_corpus import state


def test_load_missing_returns_none(tmp_path):
    assert state.load_watermark(str(tmp_path / "nope.json")) is None


def test_roundtrip(tmp_path):
    p = str(tmp_path / "wm.json")
    state.save_watermark(p, 1735660800123)
    assert state.load_watermark(p) == 1735660800123


def test_load_corrupt_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json{")
    assert state.load_watermark(str(p)) is None


def test_save_creates_parent_dirs(tmp_path):
    p = str(tmp_path / "a" / "b" / "wm.json")
    state.save_watermark(p, 42)
    assert state.load_watermark(p) == 42


def test_default_state_path_honors_env(monkeypatch):
    monkeypatch.setenv("CASS_FEED_STATE", "/tmp/x/wm.json")
    assert state.default_state_path() == "/tmp/x/wm.json"
