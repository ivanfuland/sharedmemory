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


_META = {"id": 7, "agent": "codex", "started_at": 1_700_000_000_000, "title": "t"}


def test_new_6role_labels_registered_explicitly():
    # franken 6-role(spec §2):新角色必须有显式标签,不能靠 .get(role, role) fallback 漏出裸角色名。
    # system 被 pruner drop、够不着 render,标签仍要在(防御:route 变更时不漏裸角色名)。
    for role in ("tool_call", "tool_result", "reasoning", "system"):
        assert role in render._ROLE_LABEL
        assert render._ROLE_LABEL[role] != role

def test_render_new_role_labels():
    out = render.render(_META, [
        Msg(0, "user", "问题"),
        Msg(1, "assistant", "回答"),
        Msg(2, "tool_call", "Bash: ls", tool_call_id="c1"),
        Msg(3, "tool_result", "OK", tool_call_id="c1"),
        Msg(4, "reasoning", "想了想"),
    ])
    assert "### User" in out and "### Assistant" in out
    assert "### Tool Call [#c1]" in out                      # 标签 + 配对标记
    assert "### Tool Result [#c1]" in out
    assert "### Reasoning" in out


def test_render_unpaired_result_marked():
    out = render.render(_META, [Msg(0, "tool_result", "orphan out", unpaired=True)])
    assert "### Tool Result [unpaired]" in out               # unpaired 显式标记,不留空让 gbrain 顺序脑补


def test_render_result_without_id_marked_unpaired():
    out = render.render(_META, [Msg(0, "tool_result", "no id out")])
    assert "[unpaired]" in out                               # 结果无 id → 也标 unpaired


def test_render_skips_empty_reasoning():
    out = render.render(_META, [Msg(0, "user", "hi"), Msg(1, "reasoning", "")])
    assert "### Reasoning" not in out                        # claude 空 reasoning 被跳过
