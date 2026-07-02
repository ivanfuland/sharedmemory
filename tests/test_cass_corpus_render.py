# tests/test_cass_corpus_render.py
# 渲染确定性是去重的命根子(确定性文件名+frontmatter → content_hash 稳定 → gbrain 不重合成)。
from cass_corpus import render
from cass_corpus.pruner import Msg

META = {"id": 42, "agent": "claude_code", "title": "fix bug", "workspace": "/home/x/proj",
        "started_at": 1735660800}


def test_filename_deterministic_and_stable():
    assert render.transcript_filename(META) == render.transcript_filename(META)
    assert render.transcript_filename(META).endswith("-cass-claude-code-42.md")


def test_openclaw_agent_slug_sanitized():
    m = {**META, "agent": "openclaw/wood"}
    assert "/" not in render.transcript_filename(m)


def test_render_deterministic_no_timestamp_drift():
    msgs = [Msg(0, "user", "hi"), Msg(1, "agent", "hello")]
    assert render.render(META, msgs) == render.render(META, msgs)


def test_render_has_no_dream_markers():
    t = render.render(META, [Msg(0, "user", "x"), Msg(1, "agent", "y")])
    assert "dream_generated" not in t
    assert "mode: lsd" not in t
    assert "source: cass" in t


def test_render_labels_roles_and_skips_empty():
    t = render.render(META, [Msg(0, "user", "改 bug"), Msg(1, "agent", "好"), Msg(2, "agent", "  ")])
    assert "### User" in t and "### Assistant" in t
    assert t.count("### Assistant") == 1   # 空内容 turn 跳过
