# tests/test_cass_corpus_render.py
# 渲染确定性是去重的命根子(确定性文件名+frontmatter → content_hash 稳定 → gbrain 不重合成)。
import re

from cass_corpus import render
from cass_corpus.pruner import Msg

META = {"id": 42, "agent": "claude_code", "title": "fix bug", "workspace": "/home/x/proj",
        "started_at": 1735660800}


EXT = {"external_id": "-home-ivan--openclaw/16f95b8d-ebc4-4222-898d-aaaabbbbcccc",
       "source_id": "local"}
META_X = {**META, **EXT}


def test_filename_deterministic_and_stable():
    assert render.transcript_filename(META_X) == render.transcript_filename(META_X)


# ── 稳定身份:文件名不得随 conversation_id 漂移 ──
# conversation_id 是 CASS 的 SQLite rowid,全量重摄重建库即重新发号(实测 2290/2361 会话变号)。
# 用它做文件名 → 同一会话换名 → gbrain 当新文档全量重炼 + 留下孤儿页。
# 稳定键 = (source_id, agent, external_id),即 CASS canonical 的唯一约束。

def test_filename_survives_conv_id_change():
    """同一会话在重摄后 rowid 从 42 变 4242,文件名必须不变。这是本修复的核心不变量。"""
    a = render.transcript_filename(META_X)
    b = render.transcript_filename({**META_X, "id": 4242})
    assert a == b


def test_filename_distinguishes_sessions():
    other = {**META_X, "external_id": "-home-ivan--openclaw/ffffffff-0000-0000-0000-000000000000"}
    assert render.transcript_filename(META_X) != render.transcript_filename(other)


def test_filename_key_includes_agent_and_source_id():
    """CASS 唯一约束是 (source_id, agent_id, external_id) —— 只哈希 external_id 不够。"""
    assert render.transcript_filename(META_X) != render.transcript_filename({**META_X, "agent": "codex"})
    assert render.transcript_filename(META_X) != render.transcript_filename({**META_X, "source_id": "GongShi"})


def test_rendered_bytes_survive_conv_id_change():
    """codex PR#41 P1:文件名稳定还不够 —— frontmatter 里若留 rowid,content_hash 仍漂移,
    gbrain 照样把同一会话当新内容全量重炼。**正文必须逐字节相同。**"""
    a = render.render(META_X, [Msg(0, "user", "hi")])
    b = render.render({**META_X, "id": 4242}, [Msg(0, "user", "hi")])
    assert a == b


def test_frontmatter_has_no_rowid():
    """rowid 绝不进 content-hashed frontmatter。真正的不变量由
    test_rendered_bytes_survive_conv_id_change 守;这条只防 `conversation_id` 键复活。"""
    fm = render.render(META_X, [])
    body = fm.split("---")[1]
    assert not any(l.startswith("conversation_id") for l in body.splitlines())


def test_filename_never_matches_numeric_rowid_regex():
    """裸 16 hex 有 ~2.8e-4 概率全是数字(真库 2424 条里就中了 1 条),会被下游
    `/-(\\d+)\\.md$/` 当 rowid 误捕。前缀 's' 让它永不可能。"""
    numeric = re.compile(r"-(\d+)\.md$")
    assert not numeric.search(render.transcript_filename(META_X))
    # 扫一批合成 external_id,确认没有一个能撞出纯数字末段
    for i in range(3000):
        m = {**META_X, "external_id": f"sess/{i:08d}-uuid"}
        assert not numeric.search(render.transcript_filename(m)), m["external_id"]


def test_filename_shape():
    fn = render.transcript_filename(META_X)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-cass-claude-code-s[0-9a-f]{16}\.md", fn), fn


def test_filename_legacy_fallback_without_external_id():
    """老/合成 schema 无 external_id 列 → 回退到 rowid 派生的键,仍确定性,但不稳定(有意)。"""
    fn = render.transcript_filename(META)          # META 无 external_id
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-cass-claude-code-s[0-9a-f]{16}\.md", fn), fn
    assert fn != render.transcript_filename({**META, "id": 43})   # 回退键随 id 变,如实反映"无稳定身份"


def test_frontmatter_carries_external_id_and_session_key():
    fm = render.render(META_X, [])
    assert "external_id: -home-ivan--openclaw/16f95b8d-ebc4-4222-898d-aaaabbbbcccc" in fm
    assert "source_id: local" in fm
    assert f"session_key: {render.session_key(META_X)}" in fm    # 稳定,可反查


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
