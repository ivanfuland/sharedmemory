import everos_adapter.feed as feed
from everos_adapter.cap import NoopClamper


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"request_id": "r1", "data": {"status": "extracted"}}


class _HeadCap:
    IS_TECHNICAL_DEBT = False

    def clamp(self, content, cap):
        return content[:cap]


class _TailCap:
    IS_TECHNICAL_DEBT = False

    def clamp(self, content, cap):
        return content[-cap:]


def _capture(monkeypatch):
    seen = []

    def fake_post(url, json=None, timeout=None):
        if url.endswith("/add"):
            seen.append(json)
        return _Resp()

    monkeypatch.setattr(feed.httpx, "post", fake_post)
    return seen


def test_add_payload_content_and_tool_args_redacted(monkeypatch):
    seen = _capture(monkeypatch)
    mapped = [
        {"sender_id": "agent-x", "role": "assistant", "timestamp": 1,
         "content": "leaked sk-ABC123SECRETKEY000000000000"},
        {"sender_id": "agent-x", "role": "assistant", "timestamp": 2, "content": "run",
         "tool_calls": [{"id": "t", "type": "function", "function": {
             "name": "curl", "arguments": "curl -H 'Authorization: Bearer sk-XYZSECRET99999999'"}}]},
    ]
    feed.feed_session("http://x", "s1", mapped, clamper=NoopClamper())
    blob = str(seen)
    assert "sk-ABC123SECRETKEY000000000000" not in blob
    assert "sk-XYZSECRET99999999" not in blob
    assert "REDACTED" in blob


def test_caller_input_not_mutated(monkeypatch):
    _capture(monkeypatch)
    original = {"sender_id": "agent-x", "role": "assistant", "timestamp": 1,
                "content": "sk-ABC123SECRETKEY000000000000"}
    feed.feed_session("http://x", "s1", [original], clamper=NoopClamper())
    assert original["content"] == "sk-ABC123SECRETKEY000000000000"


def test_over_500_is_split_into_multiple_add_calls(monkeypatch):
    seen = _capture(monkeypatch)
    mapped = [{"sender_id": "a", "role": "user", "timestamp": i + 1, "content": "x"} for i in range(1201)]
    feed.feed_session("http://x", "s1", mapped, clamper=NoopClamper())
    assert [len(p["messages"]) for p in seen] == [500, 500, 201]


def test_clamp_applied_to_tool_and_downgraded_synthetics_not_prose(monkeypatch):
    seen = _capture(monkeypatch)
    mapped = [
        {"sender_id": "a", "role": "tool", "timestamp": 1, "content": "y" * 9000, "tool_call_id": "t1"},
        {"sender_id": "a", "role": "assistant", "timestamp": 2, "content": "z" * 9000},          # 散文
        {"sender_id": "a", "role": "assistant", "timestamp": 3, "content": "[tool_result] " + "w" * 9000},
        {"sender_id": "a", "role": "assistant", "timestamp": 4, "content": "[tool_call] " + "v" * 9000},
    ]
    feed.feed_session("http://x", "s1", mapped, clamper=_HeadCap(), tool_result_cap=100)
    msgs = seen[0]["messages"]
    assert len(msgs[0]["content"]) == 100      # role=tool 被压
    assert len(msgs[1]["content"]) == 9000     # assistant 散文忠实保留
    assert len(msgs[2]["content"]) == 100      # 降级的 tool_result（codex R0 P2#8）
    assert len(msgs[3]["content"]) == 100      # 降级的 tool_call


def test_secret_fragment_cannot_leak_when_clamp_splits_it(monkeypatch):
    """codex R2-1：原 `test_redact_runs_after_clamp` 两种顺序都过，不验证任何东西。

    有区分度的场景：clamp **切断** secret -> redact 正则失效 -> 残留 `sk-ABC123S` 片段。
    只有 **前置 redact** 能拦住。若把 `_sanitize` 改回 `clamp -> redact`，本测试必红。
    """
    seen = _capture(monkeypatch)
    secret = "sk-ABC123SECRETKEY000000000000"
    mapped = [{"sender_id": "a", "role": "tool", "timestamp": 1,
               "content": "x" * 50 + secret, "tool_call_id": "t1"}]
    feed.feed_session("http://x", "s1", mapped, clamper=_HeadCap(), tool_result_cap=60)
    blob = str(seen)
    assert secret not in blob
    assert "sk-ABC" not in blob        # 连片段都不许出现


def test_rescue_clamp_output_carries_no_secret(monkeypatch):
    """端到端：即便 clamper 从中段重组内容（模拟 `_clamp` 硬错误行抢救），最终喂出的 blob 不含 secret。

    ⚠️ 诚实说明（Task 6 review 修正原 docstring 的失实 claim）：本测试**不能独立证明
    「后置 redact 必要」**。`_sanitize` 先做前置 redact，clamper 拿到的已是脱敏后的内容，
    无法凭空引入前置 redact 没见过的 secret；删掉后置 redact 本测试仍会绿（review 消融
    验证坐实）。它实际保证的是「rescue 型 clamp 不泄漏」这一端到端性质，通过由**前置 redact**
    承担；后置 redact 是幂等的防御性双洗（brief R2-1 保留），非本测试可证的独立必要项。

    实现注：secret 字面量从 brief 的 20 字符补到 23（`sk-XYZSECRET99999999` ->
    `sk-XYZSECRET99999999999`）——20 字符串脱离 `Bearer ` 上下文后不满足
    `cass_corpus.redact` 第 4 条规则最短长度（`sk-` 后需 >=20，原串仅 18），否则 pre/post
    都不命中、测试假红。
    """
    seen = _capture(monkeypatch)

    class _RescueCap:                  # 模拟「从中段捞出一行」的 clamper
        IS_TECHNICAL_DEBT = False
        def clamp(self, content, cap):
            return "head\n" + content.split("\n")[1] + "\ntail"

    secret = "sk-XYZSECRET99999999999"
    mapped = [{"sender_id": "a", "role": "tool", "timestamp": 1,
               "content": f"head\nERROR token {secret}\ntail", "tool_call_id": "t1"}]
    feed.feed_session("http://x", "s1", mapped, clamper=_RescueCap(), tool_result_cap=999)
    assert secret not in str(seen)
