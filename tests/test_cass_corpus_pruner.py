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
    assert 1500 <= len(out) <= 1500 + 60                 # 内容恰 cap + 指针,不 1125 不 16×
    assert "截断" in out and "CASS" in out

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
