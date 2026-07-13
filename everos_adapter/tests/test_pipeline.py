import msgpack

import everos_adapter.feed as feed
from everos_adapter.cap import NoopClamper
from everos_adapter.pipeline import prepare_session, run_session

AGENT, USER = "agent-x", "demo-owner"


def _blob(d):
    return msgpack.packb(d, use_bin_type=True)


# read_conversation 的 _EXTRA_COLS 现含 extra_bin/extra_json 两列（对齐
# test_cass_reader.py / test_role_map.py 的既定约定），extra_dict 在 extra_bin 为
# None 时会去查 row["extra_json"]，缺键即 KeyError —— 故本文件所有手写 fixture 行
# 都必须显式带 "extra_json" key（值恒为 None）。
def _call_rows(idx, tcid, ts):
    return [
        {"idx": idx, "role": "tool_call", "created_at": ts, "content": 'Bash({"command":"ls"})',
         "extra_bin": _blob({"tool_call_id": tcid, "tool_call_args": '{"command":"ls"}'}), "extra_json": None},
        {"idx": idx + 1, "role": "tool_result", "created_at": ts + 1, "content": "out",
         "extra_bin": _blob({"tool_call_id": tcid}), "extra_json": None},
    ]


def _good_rows():
    rows = [{"idx": 0, "role": "user", "created_at": 1000, "content": "修 bug", "extra_bin": None, "extra_json": None}]
    for k in range(3):
        rows += _call_rows(1 + k * 2, f"t{k}", 1001 + k * 10)
    rows.append({"idx": 99, "role": "assistant", "created_at": 2000, "content": "搞定", "extra_bin": None, "extra_json": None})
    return rows


class _Resp:
    def raise_for_status(self): pass
    def json(self): return {"request_id": "r", "data": {"status": "extracted"}}


def _capture(monkeypatch):
    seen = []
    def fake_post(url, json=None, timeout=None):
        if url.endswith("/add"):
            seen.append(json)
        return _Resp()
    monkeypatch.setattr(feed.httpx, "post", fake_post)
    return seen


def test_good_session_is_fed(monkeypatch):
    seen = _capture(monkeypatch)
    out = run_session("http://x", "s2", _good_rows(), AGENT, USER, clamper=NoopClamper())
    assert out["skipped"] is False and len(seen) == 1


def test_timestamps_are_deduped_before_add(monkeypatch):
    # ensure_unique_timestamps 真的进了管线（codex R0 P0#2）
    seen = _capture(monkeypatch)
    rows = [{"idx": 0, "role": "user", "created_at": 5, "content": "q", "extra_bin": None, "extra_json": None}]
    for k in range(3):
        rows += [
            {"idx": 1 + k * 2, "role": "tool_call", "created_at": 5, "content": 'Bash({"command":"ls"})',
             "extra_bin": _blob({"tool_call_id": f"t{k}", "tool_call_args": "{}"}), "extra_json": None},
            {"idx": 2 + k * 2, "role": "tool_result", "created_at": 5, "content": "o",
             "extra_bin": _blob({"tool_call_id": f"t{k}"}), "extra_json": None},
        ]
    rows.append({"idx": 9, "role": "assistant", "created_at": 5, "content": "done", "extra_bin": None, "extra_json": None})
    run_session("http://x", "s3", rows, AGENT, USER, clamper=NoopClamper())
    ts = [m["timestamp"] for m in seen[0]["messages"]]
    assert len(ts) == len(set(ts)) and all(x > 0 for x in ts)


def test_trailing_orphan_is_held_not_fed(monkeypatch):
    """split_feedable 真的进了管线（codex R0 P1#5）。

    codex R1 P1#2：原构造 `rows[:-1]` 去掉末条 assistant 会连带丢失可喂内容，
    `seen` 为空、`if seen:` 分支永不执行 —— 测试是假的。
    改为让末条真 assistant 大到 append 超 max_append_chars，孤儿因此独立留在尾部，
    这样才能同时验证「有喂出」与「有 held」两个断言。
    """
    seen = _capture(monkeypatch)
    rows = _good_rows()
    rows[-1]["content"] = "答" * 3990                        # 末条 assistant 撑到接近 4000
    rows.append({"idx": 100, "role": "tool_result", "created_at": 2100,
                 "content": "tail orphan", "extra_bin": None, "extra_json": None})
    out = run_session("http://x", "s4", rows, AGENT, USER, clamper=NoopClamper())

    assert out["skipped"] is False                            # 无结构门拦截，正常应喂出
    assert out["held"] == 1                                   # 尾部孤儿被 hold
    assert seen, "应发出 /add"
    assert not any("tail orphan" in (m.get("content") or "") for m in seen[0]["messages"])


def test_orphan_absorbed_into_preceding_assistant(monkeypatch):
    """absorb_orphans 真的进了管线（codex R0 P1#5）。

    codex R1 P1#3：原构造把 reasoning 插在 `_call_rows` 之后，前一条是 `role="tool"`，
    走不到 append 分支 —— 删掉 absorb_orphans 测试照样过。必须让 reasoning 的前一条
    是**真 assistant**，并断言两者合并进了同一条消息。
    """
    seen = _capture(monkeypatch)
    rows = _good_rows()
    rows.insert(-1, {"idx": 96, "role": "assistant", "created_at": 1990, "content": "初步结论", "extra_bin": None, "extra_json": None})
    rows.insert(-1, {"idx": 97, "role": "reasoning", "created_at": 1995, "content": "想了想", "extra_bin": None, "extra_json": None})
    run_session("http://x", "s5", rows, AGENT, USER, clamper=NoopClamper())

    msgs = seen[0]["messages"]
    merged = [m for m in msgs if "初步结论" in (m.get("content") or "")]
    assert len(merged) == 1
    assert "想了想" in merged[0]["content"]                    # 合并进同一条 -> 证明 absorb 生效
    assert not any((m.get("content") or "").startswith("[reasoning]") for m in msgs)


def test_prepare_session_is_pure_no_http():
    ps = prepare_session("s7", _good_rows(), AGENT, USER, clamper=NoopClamper())
    assert ps.should_feed is True and ps.skip_reason == "" and ps.held == []


def test_absorbed_orphan_is_capped_end_to_end(monkeypatch):
    """codex R1 P1#1 的端到端回归：被吸收的 orphan 必须已被压过。"""
    seen = _capture(monkeypatch)

    class _Cap:
        IS_TECHNICAL_DEBT = False
        def clamp(self, content, cap): return content[:cap]

    rows = _good_rows()
    rows.insert(-1, {"idx": 96, "role": "assistant", "created_at": 1990, "content": "结论", "extra_bin": None, "extra_json": None})
    rows.insert(-1, {"idx": 97, "role": "tool_result", "created_at": 1995,
                     "content": "z" * 9000, "extra_bin": None, "extra_json": None})     # 无 id -> orphan
    run_session("http://x", "s8", rows, AGENT, USER, clamper=_Cap(), tool_result_cap=100)

    merged = [m for m in seen[0]["messages"] if "结论" in (m.get("content") or "")][0]
    assert len(merged["content"]) < 200      # 9000 字符的 orphan 已被压到 <=100


def test_all_orphan_session_is_skipped_with_zero_http(monkeypatch):
    """run_session 的唯一 skip 分支：feedable 为空（codex R0「唯一 skip 情形是无可喂
    消息」），此前零覆盖。会话只有一条无前置真 assistant 的 reasoning 行 —— 它降级成
    synthetic assistant `[reasoning] ...`，`_is_orphan_text` 为真、absorb_orphans 无处可
    append（无 prev），split_feedable 从尾部整段判定成纯 orphan -> 整条都进 held，
    feedable 恒为空。should_feed 为 False，走 skip 分支，feed_session 完全不会被调用。

    非空判据：若把 pipeline.run_session 里 `if not ps.should_feed:` 的空判定去掉、
    直接把空 feedable 喂给 feed_session，httpx.post 至少会被调一次（/add，即便
    messages=[]）—— 这里断言 seen 为空列表，能真实拦住那个回归。
    """
    seen = _capture(monkeypatch)
    rows = [{"idx": 0, "role": "reasoning", "created_at": 1, "content": "只是想了想，没锚点",
             "extra_bin": None, "extra_json": None}]

    out = run_session("http://x", "s9", rows, AGENT, USER, clamper=NoopClamper())

    assert out["skipped"] is True
    assert out["result"] is None
    assert out["held"] == 1
    assert seen == []      # 关键断言：feed_session 从未被调用，httpx.post 零次
