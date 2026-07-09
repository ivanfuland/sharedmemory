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
    # franken 6-role: "tool_call" 是新的调用角色(legacy "tool" 已改语义为结果,见下方 P0 回归测试)
    msgs = [Msg(0, "tool_call", '{"name":"read_file","args":{"path":"x.py"}}')]
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


# --- franken 6-role 归一化(spec §2/§10-①.5):新角色 + legacy 回归 ---

def test_new_tool_result_role_truncated_over_threshold():
    # 用默认阈值(1500 字符),对齐 brief 的 ">1500 chars" 断言口径
    body = "\n".join(f"line {i} 普通输出内容填充字符填充填充填充" for i in range(80))
    assert len(body) > 1500
    msgs = [Msg(0, "tool_result", body)]
    out = DeterministicPruner().prune(msgs)
    assert len(out) == 1
    assert "截断" in out[0].content


def test_new_tool_result_role_kept_when_small():
    msgs = [Msg(0, "tool_result", "OK done")]
    out = DeterministicPruner().prune(msgs)
    assert out[0].content == "OK done"


def test_drops_system_role():
    # "system" 是 codex 新版 developer 的改名,同样是配置非情景记忆 → 整段丢
    msgs = [Msg(0, "user", "改个 bug"), Msg(1, "system", "你是…系统提示"), Msg(2, "assistant", "好的")]
    out = DeterministicPruner().prune(msgs)
    assert [m.role for m in out] == ["user", "assistant"]


def test_keeps_reasoning_present():
    msgs = [Msg(0, "user", "为什么慢"), Msg(1, "reasoning", "先看 profiler,再查热点函数"), Msg(2, "assistant", "答案")]
    out = DeterministicPruner().prune(msgs)
    assert [m.role for m in out] == ["user", "reasoning", "assistant"]   # 保留,不静默丢弃
    assert out[1].content == "先看 profiler,再查热点函数"


def test_reasoning_bounded_when_long():
    body = "\n".join(f"reasoning line {i} 填充填充填充填充填充" for i in range(80))
    assert len(body) > 1500
    msgs = [Msg(0, "reasoning", body)]
    out = DeterministicPruner().prune(msgs)
    assert len(out[0].content) < len(body)     # 超阈值 → 有界,不放任模型 thinking 无限膨胀


# --- back-compat: legacy 角色语义不变,尤其 legacy "tool" 绝不再被当调用压缩(P0) ---

def test_legacy_tool_result_role_still_truncates():
    msgs = [Msg(0, "toolResult", "x" * 200)]
    out = _p().prune(msgs)     # _p() 阈值 120
    assert "截断" in out[0].content


def test_legacy_developer_role_still_drops():
    msgs = [Msg(0, "user", "hi"), Msg(1, "developer", "老版系统提示")]
    out = DeterministicPruner().prune(msgs)
    assert [m.role for m in out] == ["user"]


def test_legacy_tool_role_never_collapsed_as_call():
    # P0 回归守卫:codex 老数据里 role="tool" 是结果不是调用,绝不能压成 "[tool call]" 丢内容
    msgs = [Msg(0, "tool", '{"name":"read_file","args":{"path":"x.py"}}')]
    out = DeterministicPruner().prune(msgs)
    assert len(out) == 1
    assert not out[0].content.startswith("[tool")
    assert "read_file" in out[0].content        # 原始结果内容仍在(未被压缩丢失)


def test_legacy_tool_role_truncated_when_large():
    # legacy tool 语义上是观察/结果 → 视同 observation,超阈值同样截断(不放任无界 dump)
    body = "\n".join(f"line {i} 普通输出内容填充字符填充填充填充" for i in range(80))
    assert len(body) > 1500
    msgs = [Msg(0, "tool", body)]
    out = DeterministicPruner().prune(msgs)
    assert "截断" in out[0].content
