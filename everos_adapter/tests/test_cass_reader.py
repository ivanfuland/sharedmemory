import json

import msgpack

from everos_adapter.cass_reader import (
    args_to_json_str,
    parse_tool_name,
    read_conversation,
    read_message,
)

# `extra_dict` 只访问 extra_cols 里列出的列，故测试传 dict row 时只需给出对应 key
COLS = ["extra_bin", "extra_json"]


def _blob(d):
    return msgpack.packb(d, use_bin_type=True)


def _row(idx=0, role="user", created_at=1, content="x", extra_bin=None, extra_json=None):
    return {"idx": idx, "role": role, "created_at": created_at, "content": content,
            "extra_bin": extra_bin, "extra_json": extra_json}


def test_parse_tool_name_three_source_formats():
    assert parse_tool_name('Bash({"command":"ls"})') == "Bash"
    assert parse_tool_name('exec_command({"cmd":"ls"})') == "exec_command"
    assert parse_tool_name('bash({"cmd":"ls","description":"x"})') == "bash"


def test_parse_tool_name_unparseable_returns_empty():
    assert parse_tool_name("no parens here") == ""
    assert parse_tool_name("") == ""
    assert parse_tool_name("123bad({})") == ""


def test_bad_msgpack_degrades_to_empty():
    # 降级由 cass_corpus.reader.extra_dict 负责（返回 None），read_message 统一成 {}
    m = read_message(_row(role="tool_result", extra_bin=b"\xff\xff\xff not msgpack"), COLS)
    assert m["tool_call_id"] == "" and m["tool_call_args"] == ""


def test_empty_shell_extra_json_degrades():
    # 实测：extra_dict 对 '{}' 返回 {} 而非 None —— 对下游等价（拿不到 id）
    m = read_message(_row(role="tool_result", extra_json="{}"), COLS)
    assert m["tool_call_id"] == ""


def test_read_message_tool_call_takes_id_and_args_from_top_level():
    # ⚠️ tool_call_args 多数是 dict（全库实测 66284），少数是 str（477 条 apply_patch 补丁原文），从不是「已序列化的 JSON 串」
    row = _row(
        idx=3, role="tool_call", created_at=1751961600300,
        content='exec_command({"cmd":"pytest -x"})',
        extra_bin=_blob({
            "timestamp": "2026-04-27T07:47:33.600Z",
            "type": "response_item",
            "payload": {"type": "function_call", "call_id": "call_XYZ"},
            "tool_call_id": "call_XYZ",
            "tool_call_args": {"cmd": "pytest -x", "workdir": "/tmp"},
        }),
    )
    m = read_message(row, COLS)
    assert m["tool_call_id"] == "call_XYZ"
    assert m["tool_name"] == "exec_command"
    # 必须是合法 JSON，不是 Python repr
    assert json.loads(m["tool_call_args"]) == {"cmd": "pytest -x", "workdir": "/tmp"}
    assert "'" not in m["tool_call_args"]           # str(dict) 会留下单引号


def test_args_to_json_str_rejects_python_repr():
    out = args_to_json_str({"cmd": "cat 中文.md"})
    assert out == '{"cmd": "cat 中文.md"}'          # ensure_ascii=False
    assert json.loads(out)["cmd"] == "cat 中文.md"
    assert args_to_json_str(None) == ""
    assert args_to_json_str('{"already":"json"}') == '{"already":"json"}'


def test_non_string_tool_call_id_is_dropped():
    # bytes/int 的 id 会渲染成 "b'abc'" 这样的垃圾，宁可当没有（对齐线2 reader 的策略）
    row = _row(role="tool_result", content="o", extra_bin=_blob({"tool_call_id": b"abc"}))
    assert read_message(row, COLS)["tool_call_id"] == ""


def test_read_message_tool_result_has_id_no_args():
    row = _row(idx=4, role="tool_result", created_at=1751961600400, content="1 passed",
               extra_bin=_blob({"timestamp": "x", "type": "response_item", "payload": {}, "tool_call_id": "call_XYZ"}))
    m = read_message(row, COLS)
    assert m["tool_call_id"] == "call_XYZ"
    assert m["tool_call_args"] == ""


def test_read_message_missing_extra_bin_yields_empty_id():
    m = read_message(_row(idx=5, role="tool_result", content="out"), COLS)
    assert m["tool_call_id"] == "" and m["content"] == "out"


def test_extra_bin_wins_over_extra_json():
    # extra_dict 的优先级：extra_bin > extra_json（真实数据两列互斥，此测试是防御）
    row = _row(idx=6, role="tool_result", content="o",
               extra_bin=_blob({"tool_call_id": "FROM_BIN"}),
               extra_json='{"tool_call_id":"FROM_JSON"}')
    assert read_message(row, COLS)["tool_call_id"] == "FROM_BIN"


def test_read_conversation_preserves_order():
    rows = [_row(idx=0, role="user", created_at=None, content="hi"),
            _row(idx=1, role="assistant", created_at=None, content="yo")]
    out = read_conversation(rows, COLS)
    assert [m["role"] for m in out] == ["user", "assistant"]


def test_missing_created_at_yields_zero_not_idx():
    # 绝不回落 idx+1：会与 ms epoch 混用量纲（codex R0 P0#3）。
    # 留 0，由 ensure_unique_timestamps 顺延填补。
    assert read_message(_row(idx=7, role="user", created_at=None, content="hi"), COLS)["timestamp"] == 0


def test_real_epoch_preserved_verbatim():
    assert read_message(_row(created_at=1751961600300), COLS)["timestamp"] == 1751961600300
