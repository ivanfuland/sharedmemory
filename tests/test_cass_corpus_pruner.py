# tests/test_cass_corpus_pruner.py
# 6-role 契约的确定性降噪:keep / clamp(预算守恒+硬错误抢救) / drop + legacy 防御映射。
from cass_corpus.pruner import Msg, DeterministicPruner


def _clamp(content, cap, rescue=True):
    return DeterministicPruner()._clamp(content, cap, rescue_errors=rescue)


# ---------- _clamp / _cap_line 不变量(Task 1) ----------
def test_clamp_passthrough_when_within_cap():
    assert _clamp("short", 1500) == "short"

def test_clamp_none_and_empty():
    assert _clamp(None, 1500) == "" and _clamp("", 1500) == ""

def test_clamp_total_conserved_no_error():
    out = _clamp("X" * 5000, 1500)
    assert len(out) <= 1500                               # 守恒:绝不超 cap(reserve-upfront)
    assert 1000 <= len(out)                                # head/tail 用满预留正文(~3/4 cap),非退化
    assert "截断" in out and "CASS" in out

def test_clamp_no_hard_error_lost_at_head_boundary():
    # codex PR P1:head 边界附近(位置 850)的硬错误不因抢救/收缩被静默丢
    content = ("A" * 850 + "ERROR_HEAD_BOUNDARY_MARK\n"
               + "B" * 300 + "ERROR_MID_LONG_" + "M" * 287 + "\n"
               + "C" * 3000)
    out = _clamp(content, 1500)
    assert "ERROR_HEAD_BOUNDARY_MARK" in out              # 不丢(旧 shrink 版会丢)

def test_clamp_total_output_within_cap():
    # 总输出(含指针/标记/换行)≤ cap —— marker 已预留(codex PR R1 P2)
    content = "ERROR line here\n" * 100 + "x" * 5000
    for cap in (200, 800, 1500, 4000):
        assert len(_clamp(content, cap)) <= cap

def test_clamp_rescues_dropped_region_error():
    # 被丢弃中段的硬错误被抢救(稳健用例,与精确边界无关)
    content = "A" * 4000 + "\nERROR_DEEP_IN_MIDDLE line\n" + "B" * 4000
    assert "ERROR_DEEP_IN_MIDDLE" in _clamp(content, 1500)

def test_clamp_head_visible_error_not_reduplicated_and_mid_rescued():
    # codex PR R2 P1:head 里已可见的 ERROR 不被重复抢救、不白占预算;真·中段 ERROR 仍抢救
    content = ("A" * 100 + "ERROR_IN_HEAD" + "X" * 2000 + "\n"
               + "ERROR_REAL_MID_" + "Y" * 80 + "\n" + "B" * 5000)
    out = _clamp(content, 1500)
    assert out.count("ERROR_IN_HEAD") == 1           # head 可见,不重复
    assert "ERROR_REAL_MID_" in out                   # 预算没被 head 那条偷走,中段错误保住

def test_clamp_rescues_later_error_in_giant_line():
    # codex PR R3 P1:巨型单行第一个 ERROR 在 head 可见,后面 ERROR 落被丢弃区 → 后者仍被抢救
    content = "ERROR_HEAD_VIS" + "X" * 2000 + "ERROR_MID_DROPPED" + "Y" * 4000
    assert "ERROR_MID_DROPPED" in _clamp(content, 1500)

def test_clamp_total_conserved_with_giant_error_lines():
    content = "H" * 150 + "\n" + "\n".join(f"ERROR {i} " + "Z" * 10000 for i in range(10)) + "\n" + "T" * 5000
    out = _clamp(content, 1500)
    assert len(out) <= 1500 + 400
    assert "Z" * 10000 not in out

def test_clamp_rescues_deep_middle_error():
    lines = [f"line {i} 普通填充" for i in range(200)]
    lines[100] = 'ERROR: relation "minion_jobs" does not exist'
    out = _clamp("\n".join(lines), 1500)
    assert 'relation "minion_jobs"' in out

def test_clamp_rescues_short_error_after_oversized():
    lines = [f"pad {i}" for i in range(300)]
    lines[100] = "ERROR " + "Z" * 5000
    lines[150] = 'ERROR short relation "x" missing'
    out = _clamp("\n".join(lines), 800)
    assert 'relation "x" missing' in out                  # continue 而非 break,短错误不被挡

def test_clamp_no_duplicate_rescued_error():
    out = _clamp("A" * 3000 + "\nERROR_UNIQUE_MARKER_XYZ here\n" + "B" * 3000, 1500)
    assert out.count("ERROR_UNIQUE_MARKER_XYZ") == 1      # 抢救行不重复(codex plan R1 P2)
    near = _clamp("A" * 760 + "\nERROR_DUPLICATE_MARKER\n" + "B" * 5000, 1500)
    assert near.count("ERROR_DUPLICATE_MARKER") == 1

def test_clamp_rescues_error_keyword_deep_in_giant_line():
    content = "A" * 1200 + "\n" + "P" * 400 + "ERROR_LATE_MARKER" + "Z" * 4000 + "\n" + "B" * 1000
    assert "ERROR_LATE_MARKER" in _clamp(content, 1500)   # 关键词深埋巨行仍保住(codex plan R2 P2)

def test_clamp_reasoning_no_error_rescue():
    out = _clamp("\n".join("assert the invariant holds" for _ in range(200)), 1500, rescue=False)
    assert "硬错误行" not in out

def test_cap_line_never_exceeds_max():
    p = DeterministicPruner()
    assert len(p._cap_line("ERROR " + "Z" * 10000)) <= p.MAX_ERR_LINE

def test_clamp_cap_floor_no_full_passthrough():
    out = _clamp("Q" * 5000, 0)
    assert len(out) <= DeterministicPruner.MIN_CAP + 120


# ---------- 6-role 路由(Task 2) ----------
def test_user_and_assistant_kept_verbatim():
    out = DeterministicPruner().prune([Msg(0, "user", "订单超时"), Msg(1, "assistant", "字段 X 当 Y 用了")])
    assert [m.content for m in out] == ["订单超时", "字段 X 当 Y 用了"]

def test_system_dropped():
    out = DeterministicPruner().prune([Msg(0, "system", "你是…系统样板"), Msg(1, "user", "hi")])
    assert [m.role for m in out] == ["user"]

def test_tool_result_clamped_not_collapsed():
    body = "\n".join(f"line {i} 输出" for i in range(500))
    out = DeterministicPruner(tool_result_cap=300).prune([Msg(0, "tool_result", body)])
    c = out[0].content
    assert c != "[tool call]" and len(c) < len(body) and "截断" in c

def test_tool_call_command_preserved_in_head():
    content = "Bash: git checkout -b feat/x\n" + "BODY " * 2000
    out = DeterministicPruner(tool_call_cap=300).prune([Msg(0, "tool_call", content)])
    assert "git checkout -b feat/x" in out[0].content       # 命令在首行 → head 保住

def test_reasoning_clamped_with_rescue_off():
    content = "\n".join("assert invariant" for _ in range(300))
    out = DeterministicPruner(reasoning_cap=200).prune([Msg(0, "reasoning", content)])
    assert "硬错误行" not in out[0].content                   # reasoning 关抢救,不误标
    assert len(out[0].content) < len(content)

def test_reasoning_empty_stays_empty():
    out = DeterministicPruner().prune([Msg(0, "reasoning", "")])
    assert out[0].content == ""                              # claude 空 reasoning → clamp("")="" (render 会跳过)


# ---------- legacy 防御映射(codex R0/R1) ----------
def test_legacy_tool_clamped_not_collapsed():
    # 迁移前 role=tool = 工具输出,绝不 collapse 成 [tool call]
    body = "Chunk ID: abc\nProcess exited 0\nOutput:\n" + "\n".join(f"src line {i}" for i in range(500))
    out = DeterministicPruner(tool_result_cap=300).prune([Msg(0, "tool", body)])
    assert "[tool call]" not in out[0].content and "截断" in out[0].content

def test_legacy_developer_and_events_dropped():
    msgs = [Msg(0, "developer", "20000字系统注入"), Msg(1, "error", "x"), Msg(2, "info", "y"), Msg(3, "user", "hi")]
    assert [m.role for m in DeterministicPruner().prune(msgs)] == ["user"]

def test_legacy_agent_and_gemini_kept():
    out = DeterministicPruner().prune([Msg(0, "agent", "旧 assistant"), Msg(1, "gemini", "gemini 内容")])
    assert [m.content for m in out] == ["旧 assistant", "gemini 内容"]

def test_all_known_db_roles_mapped_no_unknown_warn():
    # spec §9①:route 表须覆盖当前库全部 role(codex 实测 8 个)+ 6-role,无一落 unknown warn
    known = ["user", "assistant", "tool_call", "tool_result", "reasoning", "system",   # 6-role
             "agent", "developer", "error", "gemini", "info", "tool", "toolResult"]     # legacy/当前库实测
    warned = []
    DeterministicPruner(warn=warned.append).prune([Msg(i, r, "c") for i, r in enumerate(known)])
    assert warned == []                                     # 无一 role 落进 unknown warn(否则新增 role 未映射)

def test_unknown_role_kept_with_loud_warn():
    warned = []
    out = DeterministicPruner(warn=warned.append).prune([Msg(0, "brand_new_role", "内容")])
    assert out[0].content == "内容"                          # 不静默丢
    assert warned and "brand_new_role" in warned[0]          # loud warn 触发


def test_pairing_fields_preserved_through_prune():
    out = DeterministicPruner(tool_call_cap=200).prune(
        [Msg(0, "tool_call", "Bash: ls", tool_call_id="c1"),
         Msg(1, "tool_result", "OK", tool_call_id="c1")])
    assert out[0].tool_call_id == "c1" and out[1].tool_call_id == "c1"
