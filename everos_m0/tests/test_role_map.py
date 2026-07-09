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
    assert len(tr) == 1 and tr[0]["tool_call_id"] == "tc1"


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


def test_message_count_six_of_nine():
    # 9 条输入 - 1 system(drop) = 8 条输出；reasoning/unpaired 转 synthetic 不丢
    assert len(_mapped()) == 8
