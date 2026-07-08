# tests/test_cass_corpus_pruner.py
# 接地的确定性清洗规则(理论依据:观察压缩/决定保留 §3.1、忠实压缩 §3.2、
# 系统提示=配置非情景记忆 §6、工具输出关键词感知截断+指针 §4.1-4.3;gbrain deterministic-collectors)。
# Pruner 是可替换接口;这里测默认 DeterministicPruner。
from cass_corpus.pruner import Msg, DeterministicPruner

import re
from cass_corpus.pruner import DeterministicPruner


def _clamp(content, cap, rescue=True):
    return DeterministicPruner()._clamp(content, cap, rescue_errors=rescue)


def _kept_len(content, cap, rescue=True):
    # 保留"内容"总量 = 输出减去指针/标记的固定开销;这里直接用不变量的等价判定:
    # 构造无标记场景下 output ≈ cap。稳妥起见断言 output 长度在 cap 量级(≤ cap + 120 标记预算)。
    return len(_clamp(content, cap, rescue))


def test_clamp_passthrough_when_within_cap():
    assert _clamp("short", 1500) == "short"


def test_clamp_none_and_empty():
    assert _clamp(None, 1500) == ""
    assert _clamp("", 1500) == ""


def test_clamp_total_conserved_no_error():
    # 无 ERROR:head+tail 拿满 cap(回流),内容部分恰 = cap,输出 = cap + 指针(~30)
    out = _clamp("X" * 5000, 1500)
    assert 1500 <= len(out) <= 1500 + 60                 # 紧边界:内容恰 cap,不再 1125(75%)也不 16×
    assert "截断" in out and "CASS" in out               # 指针在


def test_clamp_total_conserved_with_giant_error_lines():
    # 10 条含 ERROR 的巨行:不得 16× cap
    content = "H" * 150 + "\n" + "\n".join(f"ERROR {i} " + "Z" * 10000 for i in range(10)) + "\n" + "T" * 5000
    out = _clamp(content, 1500)
    assert len(out) <= 1500 + 400                        # 抢救块入预算,总量守恒(留标记余量)
    assert "Z" * 10000 not in out                        # 单行被 _cap_line 截,不整条绕过


def test_clamp_rescues_deep_middle_error():
    lines = [f"line {i} 普通填充" for i in range(200)]
    lines[100] = 'ERROR: relation "minion_jobs" does not exist'
    out = _clamp("\n".join(lines), 1500)
    assert 'relation "minion_jobs"' in out                # 深埋短 ERROR 被抢救(cap=1500,rescue_budget=375 容得下)


def test_clamp_rescues_short_error_after_oversized():
    # 低 cap:超预算的长 ERROR 在前、短 ERROR 在后 → 长的跳过、短的仍被救(continue 而非 break,codex plan R0 P1)
    lines = [f"pad {i}" for i in range(300)]
    lines[100] = "ERROR " + "Z" * 5000                   # 长错误(cap_line 到 300,但 > rescue_budget=200)
    lines[150] = 'ERROR short relation "x" missing'      # 短错误,应被救
    out = _clamp("\n".join(lines), 800)                  # cap=800 → rescue_budget=200
    assert 'relation "x" missing' in out                 # 短错误没被前面的长错误 break 挡掉


def test_clamp_no_duplicate_rescued_error():
    # 抢救行不因第二遍 head/tail 收缩而重复(先划 full-cap 再缩,codex plan R1 P2)
    out = _clamp("A" * 3000 + "\nERROR_UNIQUE_MARKER_XYZ here\n" + "B" * 3000, 1500)
    assert out.count("ERROR_UNIQUE_MARKER_XYZ") == 1
    near = _clamp("A" * 760 + "\nERROR_DUPLICATE_MARKER\n" + "B" * 5000, 1500)   # 近 head 边界例
    assert near.count("ERROR_DUPLICATE_MARKER") == 1


def test_clamp_rescues_error_keyword_deep_in_giant_line():
    # 巨型单行里 ERROR 关键词在 300 字后 → _cap_line 以关键词为中心取窗,保住它(codex plan R2 P2)
    content = "A" * 1200 + "\n" + "P" * 400 + "ERROR_LATE_MARKER" + "Z" * 4000 + "\n" + "B" * 1000
    assert "ERROR_LATE_MARKER" in _clamp(content, 1500)


def test_clamp_reasoning_no_error_rescue():
    # rescue_errors=False:思考散文里的 assert/fail 不当报错、不带"硬错误行"标记
    content = "\n".join("assert the invariant holds" for _ in range(200))
    out = _clamp(content, 1500, rescue=False)
    assert "硬错误行" not in out


def test_cap_line_never_exceeds_max():
    p = DeterministicPruner()
    assert len(p._cap_line("ERROR " + "Z" * 10000)) <= p.MAX_ERR_LINE   # 含 "…" 后 ≤300,不越界成 301


def test_clamp_cap_floor_no_full_passthrough():
    # cap=0 被抬到 MIN_CAP,不退化成输出整段
    content = "Q" * 5000
    out = _clamp(content, 0)
    assert len(out) < 5000
    assert len(out) <= DeterministicPruner.MIN_CAP + 120


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
