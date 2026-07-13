"""6-role → EverOS `/add` 三形态映射（消费 cass_reader 的扁平输出）。

EverOS 底层只认三种形态（`_to_conversation_item`）：
  ChatMessage(user/assistant 文本) / ToolCallRequest(assistant+tool_calls) / ToolCallResult(tool+tool_call_id)
其余一律 fall-through raise。

owner 归属：EverOS 的 ToolCallResult 只带 tool_call_id/content/timestamp、**丢 sender**，
故 agent owner 只能来自 assistant/tool_call 的 sender_id → 适配器强制它们 = agent_id。
"""

import msgpack

from everos_adapter.cass_reader import read_conversation
from everos_adapter.role_map import map_to_add_messages

AGENT = "agent-x"
USER = "demo-owner"

# read_conversation/read_message 需要 extra_cols 显式列出可用列（真实 query 出的列集），
# 对齐 test_cass_reader.py 的约定；fixture 行需同时携带 extra_bin/extra_json 两个 key
# （即便值恒为 None）——extra_dict 在 extra_bin 为 None 时会去查 row["extra_json"]，缺键即 KeyError。
COLS = ["extra_bin", "extra_json"]


def _blob(d):
    return msgpack.packb(d, use_bin_type=True)


def _rows():
    return [
        {"idx": 0, "role": "user", "created_at": 1, "content": "帮我修 test_foo", "extra_bin": None, "extra_json": None},
        {"idx": 1, "role": "reasoning", "created_at": 2, "content": "先跑一遍", "extra_bin": None, "extra_json": None},
        {"idx": 2, "role": "assistant", "created_at": 3, "content": "我先跑测试。", "extra_bin": None, "extra_json": None},
        {"idx": 3, "role": "tool_call", "created_at": 4, "content": 'Bash({"command":"pytest -x"})',
         "extra_bin": _blob({"tool_call_id": "t1", "tool_call_args": {"command": "pytest -x"}}), "extra_json": None},
        {"idx": 4, "role": "tool_result", "created_at": 5, "content": "E assert 1 == 2",
         "extra_bin": _blob({"tool_call_id": "t1"}), "extra_json": None},
        {"idx": 5, "role": "tool_result", "created_at": 6, "content": "orphan output", "extra_bin": None, "extra_json": None},
        {"idx": 6, "role": "system", "created_at": 7, "content": "permission-mode: acceptEdits", "extra_bin": None, "extra_json": None},
        {"idx": 7, "role": "assistant", "created_at": 8, "content": "改好了。", "extra_bin": None, "extra_json": None},
    ]


def _mapped():
    return map_to_add_messages(read_conversation(_rows(), COLS), agent_id=AGENT, user_sender=USER)


def test_system_dropped():
    assert all("permission-mode" not in (m.get("content") or "") for m in _mapped())


def test_roles_only_three_shapes():
    for m in _mapped():
        assert m["role"] in ("user", "assistant", "tool")


def test_tool_call_id_and_name_come_from_reader():
    import json as _json
    call = [m for m in _mapped() if m.get("tool_calls")][0]
    tc = call["tool_calls"][0]
    assert tc["id"] == "t1"
    assert tc["function"]["name"] == "Bash"
    # reader 已把 dict 形态的 args 转成合法 JSON 字符串
    args = tc["function"]["arguments"]
    assert isinstance(args, str) and _json.loads(args) == {"command": "pytest -x"}
    assert call["sender_id"] == AGENT


def test_paired_tool_result_is_role_tool_with_id():
    tr = [m for m in _mapped() if m["role"] == "tool"]
    assert len(tr) == 1 and tr[0]["tool_call_id"] == "t1"


def test_tool_result_without_id_becomes_synthetic_assistant():
    syn = [m for m in _mapped() if "[tool_result]" in (m.get("content") or "")]
    assert syn and syn[0]["role"] == "assistant" and syn[0]["sender_id"] == AGENT
    assert not any(m["role"] == "tool" and not m.get("tool_call_id") for m in _mapped())


def test_reasoning_becomes_synthetic_assistant_not_user():
    r = [m for m in _mapped() if "[reasoning]" in (m.get("content") or "")]
    assert r and r[0]["role"] == "assistant" and r[0]["sender_id"] == AGENT


def test_user_sender_is_human():
    u = [m for m in _mapped() if m["role"] == "user"]
    assert u and u[0]["sender_id"] == USER


def test_all_assistant_sender_is_agent():
    for m in _mapped():
        if m["role"] == "assistant":
            assert m["sender_id"] == AGENT


def test_unknown_role_skipped_conservatively():
    rows = [{"idx": 0, "role": "gemini", "created_at": 1, "content": "野 role", "extra_bin": None, "extra_json": None}]
    assert map_to_add_messages(read_conversation(rows, COLS), agent_id=AGENT, user_sender=USER) == []


def test_only_system_is_dropped():
    assert len(_mapped()) == len(_rows()) - 1


def test_tool_call_with_empty_name_still_emits_valid_dto():
    rows = [{"idx": 0, "role": "tool_call", "created_at": 1, "content": "weird content",
             "extra_bin": _blob({"tool_call_id": "t9"}), "extra_json": None}]
    out = map_to_add_messages(read_conversation(rows, COLS), agent_id=AGENT, user_sender=USER)
    fn = out[0]["tool_calls"][0]["function"]
    assert fn["name"] == "" and isinstance(fn["arguments"], str)


def test_tool_call_without_id_becomes_synthetic_not_empty_id_request():
    # 空 id 的 ToolCallRequest 会污染 EverOS 配对逻辑（codex R0 P1#6）
    rows = [{"idx": 0, "role": "tool_call", "created_at": 1,
             "content": 'Bash({"command":"ls"})', "extra_bin": None, "extra_json": None}]
    out = map_to_add_messages(read_conversation(rows, COLS), agent_id=AGENT, user_sender=USER)
    assert not any(m.get("tool_calls") for m in out)
    assert out[0]["role"] == "assistant" and out[0]["content"].startswith("[tool_call]")


def test_timestamps_are_ints_and_positive():
    assert all(isinstance(m["timestamp"], int) and m["timestamp"] > 0 for m in _mapped())
