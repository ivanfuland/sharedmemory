# tests/test_cass_corpus_state.py
import pytest
from cass_corpus import state


def test_load_missing_returns_none(tmp_path):
    assert state.load_cursor(str(tmp_path / "nope.json")) is None


def test_roundtrip(tmp_path):
    p = str(tmp_path / "wm.json")
    state.save_cursor(p, 1735660800123, 42)
    assert state.load_cursor(p) == (1735660800123, 42)


def test_corrupt_json_raises(tmp_path):
    # codex PR#27 P1-B: 坏文件不能静默当首跑,必须 fail loud
    p = tmp_path / "bad.json"
    p.write_text("not json{")
    with pytest.raises(Exception):
        state.load_cursor(str(p))


def test_missing_ts_field_raises(tmp_path):
    p = tmp_path / "nf.json"
    p.write_text('{"foo": 1}')
    with pytest.raises(Exception):
        state.load_cursor(str(p))


def test_save_creates_parent_dirs(tmp_path):
    p = str(tmp_path / "a" / "b" / "wm.json")
    state.save_cursor(p, 42, 7)
    assert state.load_cursor(p) == (42, 7)


def test_default_state_path_honors_env(monkeypatch):
    monkeypatch.setenv("CASS_FEED_STATE", "/tmp/x/wm.json")
    assert state.default_state_path() == "/tmp/x/wm.json"
