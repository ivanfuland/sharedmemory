# tests/test_cass_corpus_pruner.py
# 接地的确定性清洗规则(理论依据:观察压缩/决定保留 §3.1、忠实压缩 §3.2、
# 系统提示=配置非情景记忆 §6、工具输出关键词感知截断+指针 §4.1-4.3;gbrain deterministic-collectors)。
# Pruner 是可替换接口;这里测默认 DeterministicPruner。
from cass_corpus.pruner import Msg, DeterministicPruner


def _p(**kw):
    # 低阈值便于触发截断(按 char 计,token≈char/3.5;阈值传 char 上限直接控)
    return DeterministicPruner(tool_result_max_chars=120, head_lines=2, tail_lines=2, max_line_chars=80, **kw)


def test_drops_developer_role():
    msgs = [Msg(0, "user", "改个 bug"), Msg(1, "developer", "你是…20000字系统样板"), Msg(2, "agent", "好的")]
    out = _p().prune(msgs)
    assert [m.role for m in out] == ["user", "agent"]   # developer 整段丢


def test_keeps_user_and_assistant_verbatim():
    msgs = [Msg(0, "user", "订单服务老超时"), Msg(1, "agent", "字段 X 被当作字段 Y 用了")]
    out = _p().prune(msgs)
    assert out[0].content == "订单服务老超时"            # 决定/意图忠实保留,不压
    assert out[1].content == "字段 X 被当作字段 Y 用了"


def test_collapses_tool_call_to_one_line():
    msgs = [Msg(0, "tool", '{"name":"read_file","args":{"path":"x.py"}}')]
    out = _p().prune(msgs)
    assert len(out) == 1
    assert out[0].content.startswith("[tool")             # 压成一行标记
    assert "read_file" in out[0].content                  # 保留工具名
    assert len(out[0].content) < 60


def test_small_tool_result_kept_as_is():
    msgs = [Msg(0, "toolResult", "OK done")]
    out = _p().prune(msgs)
    assert out[0].content == "OK done"                    # 小于阈值 → 原样


def test_large_tool_result_truncated_with_pointer():
    body = "\n".join(f"line {i} 普通输出内容填充字符" for i in range(50))
    msgs = [Msg(0, "toolResult", body)]
    out = _p().prune(msgs)
    c = out[0].content
    assert len(c) < len(body)                              # 确实变短
    assert "line 0" in c and "line 49" in c                # 首尾保留
    assert "line 25" not in c                              # 中间普通行被砍
    assert "截断" in c or "truncated" in c.lower()         # 指针标记在
    assert "CASS" in c or "原始" in c                       # 指向原文的 pointer


def test_keyword_lines_survive_truncation():
    lines = [f"line {i} 普通填充内容" for i in range(50)]
    lines[25] = "line 25 ERROR: NullPointerException at foo.py:42"
    msgs = [Msg(0, "toolResult", "\n".join(lines))]
    out = _p().prune(msgs)
    assert "ERROR: NullPointerException" in out[0].content  # 关键行即使在中间也保留(§4.3 关键词采样)


def test_giant_single_line_truncated_by_chars_no_crash():
    msgs = [Msg(0, "toolResult", "X" * 50000)]             # 单行巨型(JSON/文件 dump)
    out = _p().prune(msgs)
    assert len(out[0].content) < 50000                     # 按字符截,不崩
