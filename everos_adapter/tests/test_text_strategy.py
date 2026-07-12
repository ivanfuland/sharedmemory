from everos_adapter.text_strategy import absorb_orphans, split_feedable

A = "agent-x"


def _asst(t, c):
    return {"sender_id": A, "role": "assistant", "timestamp": t, "content": c}


def _user(t, c):
    return {"sender_id": "demo-owner", "role": "user", "timestamp": t, "content": c}


def _orphan(t, c):
    return {"sender_id": A, "role": "assistant", "timestamp": t, "content": f"[tool_result] {c}"}


def _reason(t, c):
    return {"sender_id": A, "role": "assistant", "timestamp": t, "content": f"[reasoning] {c}"}


def _call(t, i):
    return {
        "sender_id": A, "role": "assistant", "timestamp": t, "content": "Bash({})",
        "tool_calls": [{"id": i, "type": "function", "function": {"name": "Bash", "arguments": "{}"}}],
    }


def test_orphan_appends_to_preceding_real_assistant():
    out = absorb_orphans([_user(1, "q"), _asst(2, "思考中"), _orphan(3, "孤儿输出")])
    assert len(out) == 2
    assert "思考中" in out[1]["content"] and "孤儿输出" in out[1]["content"]


def test_orphan_without_preceding_assistant_stays_synthetic():
    out = absorb_orphans([_user(1, "q"), _orphan(2, "孤儿")])
    assert len(out) == 2 and out[1]["role"] == "assistant" and out[1]["sender_id"] == A


def test_consecutive_orphans_coalesce_into_one():
    out = absorb_orphans([_user(1, "q"), _orphan(2, "a"), _reason(3, "b"), _orphan(4, "c")])
    synth = [m for m in out if m["role"] == "assistant"]
    assert len(synth) == 1
    for frag in ("a", "b", "c"):
        assert frag in synth[0]["content"]


def test_orphan_never_appends_to_tool_call_message():
    # tool_call 已挂 tool_calls[]，append 会污染 ToolCallRequest 的 content
    out = absorb_orphans([_user(1, "q"), _call(2, "t1"), _orphan(3, "孤儿")])
    call = [m for m in out if m.get("tool_calls")][0]
    assert "孤儿" not in call["content"]
    assert any("[tool_result]" in (m.get("content") or "") for m in out if not m.get("tool_calls"))


def test_append_respects_max_chars():
    out = absorb_orphans([_user(1, "q"), _asst(2, "x" * 3990), _orphan(3, "y" * 100)], max_append_chars=4000)
    assert len(out) == 3  # 超限 -> 不 append，另起 synthetic


def test_absorbed_orphan_is_clamped_before_append():
    # codex R1 P1#1：append 后前缀丢失，出口 cap 检测不到 -> 必须 append 前压
    out = absorb_orphans([_user(1, "q"), _asst(2, "答"), _orphan(3, "y" * 9000)], clamp_fn=lambda c: c[:50])
    assert len(out) == 2                          # 被吸收
    assert len(out[1]["content"]) <= 1 + 1 + 50   # "答" + \n + 压过的 50
    assert not out[1]["content"].startswith("[tool_result]")   # 证明前缀确实丢了


def test_independent_orphan_also_clamped():
    out = absorb_orphans([_user(1, "q"), _orphan(2, "y" * 9000)], clamp_fn=lambda c: c[:50])
    assert len(out) == 2 and len(out[1]["content"]) == 50


def test_trailing_pure_orphan_is_held_not_fed():
    fed, held = split_feedable(absorb_orphans([_user(1, "q"), _call(2, "t1"), _orphan(3, "尾部孤儿")]))
    assert len(held) == 1 and "[tool_result]" in held[0]["content"]
    assert fed[-1].get("tool_calls")


def test_split_feedable_holds_only_trailing_orphans():
    """codex R2-2/R2-3：原来的两个测试只断言 `held == []`，
    一个返回 `(mapped, [])` 的 no-op `split_feedable` 也会通过 —— 无效测试。
    必须有**正例**（该 hold 时真的 hold）才能证伪 no-op。
    """
    # 正例：尾部两个孤儿 coalesce 成一条，被 hold
    absorbed = absorb_orphans([_user(1, "q"), _call(2, "t1"), _orphan(3, "a"), _orphan(4, "b")])
    fed, held = split_feedable(absorbed)
    assert len(held) == 1                                   # no-op 会得到 0 -> 红
    assert held[0]["content"].startswith("[tool_result]")
    assert not any((m.get("content") or "").startswith("[tool_result]") for m in fed)

    # 负例：真锚在后，不误 hold
    fed2, held2 = split_feedable(absorb_orphans([_user(1, "q"), _call(2, "t1"), _orphan(3, "孤儿"), _asst(4, "结论")]))
    assert held2 == [] and fed2[-1]["content"] == "结论"

    # 负例：孤儿被吸收进真 assistant，无残留
    fed3, held3 = split_feedable(absorb_orphans([_user(1, "q"), _asst(2, "答"), _orphan(3, "尾部孤儿")]))
    assert held3 == [] and "尾部孤儿" in fed3[-1]["content"]
