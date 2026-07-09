"""6-role → EverOS `/add` 三形态映射。

EverOS 底层只认三种形态（`_to_conversation_item`）：
  ChatMessage(user/assistant 文本) / ToolCallRequest(assistant+tool_calls) / ToolCallResult(tool+tool_call_id)
其余一律 fall-through raise。所以 6 个 role 必须压进这三种，映射是 6→3+结构，非 1:1。

owner 归属：EverOS 的 ToolCallResult 只带 tool_call_id/content/timestamp、**丢 sender**，
故 agent owner 只能来自 assistant/tool_call 的 sender_id → 适配器强制它们 = agent_id。
"""

import json
import pathlib

from everos_m0.role_map import map_to_add_messages

FIX = json.loads((pathlib.Path(__file__).parent.parent / "fixtures/synthetic_session.json").read_text())

AGENT = "ivan-coding"
USER = "ivan"


def _mapped():
    return map_to_add_messages(FIX["messages"], agent_id=AGENT, user_sender=USER)


def test_system_dropped():
    assert all("permission-mode" not in (m.get("content") or "") for m in _mapped())


def test_roles_only_three_shapes():
    for m in _mapped():
        assert m["role"] in ("user", "assistant", "tool")


def test_tool_call_becomes_assistant_with_tool_calls():
    m = [x for x in _mapped() if x["role"] == "assistant" and x.get("tool_calls")]
    assert m and m[0]["tool_calls"][0]["function"]["name"] == "bash"
    assert m[0]["sender_id"] == AGENT


def test_paired_tool_result_is_role_tool_with_id():
    tr = [x for x in _mapped() if x["role"] == "tool"]
    # 7 个 paired result 各自带回自己的 id；unpaired 的那条不在此列
    assert [x["tool_call_id"] for x in tr] == ["tc1", "tc2", "tc3", "tc4", "tc5", "tc6", "tc7"]


def test_unpaired_tool_result_becomes_synthetic_assistant():
    syn = [x for x in _mapped() if x["role"] == "assistant" and "[tool_result:" in (x.get("content") or "")]
    assert syn and syn[0]["sender_id"] == AGENT


def test_reasoning_becomes_synthetic_assistant_not_user():
    r = [x for x in _mapped() if "[reasoning]" in (x.get("content") or "")]
    assert r and r[0]["role"] == "assistant" and r[0]["sender_id"] == AGENT


def test_no_role_tool_without_id():
    assert all(x.get("tool_call_id") for x in _mapped() if x["role"] == "tool")


def test_user_sender_is_human():
    u = [x for x in _mapped() if x["role"] == "user"]
    assert u and u[0]["sender_id"] == USER


def test_tool_call_args_coerced_to_json_string():
    # EverOS ToolCallDTO.arguments 必须是 str；fixture 的 tc2 用 dict args
    calls = [tc for x in _mapped() if x.get("tool_calls") for tc in x["tool_calls"]]
    edit = [tc for tc in calls if tc["function"]["name"] == "edit"]
    assert edit, "fixture 应含 dict-args 的 edit tool_call"
    args = edit[0]["function"]["arguments"]
    assert isinstance(args, str) and '"path"' in args


def test_all_assistant_and_tool_call_sender_is_agent():
    # agent owner 完全依赖这个不变式（tool_result 的 sender 被 EverOS 丢弃）
    for m in _mapped():
        if m["role"] == "assistant":
            assert m["sender_id"] == AGENT


def test_unknown_role_skipped_conservatively():
    msgs = [{"role": "gemini", "content": "野 role", "extra_json": {}, "timestamp": 1}]
    assert map_to_add_messages(msgs, agent_id=AGENT, user_sender=USER) == []


def test_timestamps_preserved_and_ordered():
    out = _mapped()
    ts = [m["timestamp"] for m in out]
    assert ts == sorted(ts) and all(isinstance(t, int) for t in ts)


def test_only_system_is_dropped():
    # 只有 system 被 drop；reasoning / unpaired tool_result 转 synthetic assistant，不丢
    assert len(_mapped()) == len(FIX["messages"]) - 1


# ── everalgo AgentCaseExtractor 的准入门槛（实测 everalgo/agent_memory/case.py）──
# EverOS 构造 AgentCaseExtractor 时不传参 → min_tool_call_rounds 走默认 3，
# 且不可经 everos.toml / ome.toml 配置。fixture 若不满足，agent_case 会被静默
# 跳过（日志 agent_case_skipped_by_algo），端到端冒烟就会神秘 TIMEOUT。
# 把门槛钉成断言，fixture 被改坏时这里先红。


def test_fixture_has_enough_tool_call_rounds_for_agent_case():
    rounds = sum(1 for m in _mapped() if m.get("tool_calls"))
    assert rounds >= 3, f"agent_case 需 >=3 轮 tool-call，fixture 只有 {rounds}"


def test_fixture_last_message_is_assistant_prose():
    # _should_skip: "Incomplete agent trajectory (last message is not a final
    # assistant response)" —— 末条必须是 assistant 的 ChatMessage
    last = _mapped()[-1]
    assert last["role"] == "assistant" and not last.get("tool_calls")


def test_fixture_has_user_anchor():
    # _strip_before_first_user 会丢掉首条 user 之前的一切；无 user 则整个 cell 被跳过
    assert _mapped()[0]["role"] == "user"
