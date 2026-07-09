"""证明 `/add` 的 outbound payload 真的脱敏过。

不测「_redact_msg 返回值对不对」——那只证明函数自己没问题。拦住 httpx.post，
检查**真正要发出网络的那个 dict**，因为泄漏发生在网络边界，不在函数边界。
"""

import everos_m0.feed_one as feed


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"request_id": "r1", "data": {"status": "extracted"}}


def _capture(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        if url.endswith("/add"):
            captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(feed.httpx, "post", fake_post)
    return captured


def test_add_payload_content_redacted(monkeypatch):
    captured = _capture(monkeypatch)
    mapped = [
        {
            "sender_id": "ivan-coding",
            "role": "assistant",
            "timestamp": 1,
            "content": "leaked sk-ABC123SECRETKEY000000000000",
        }
    ]
    feed.feed_session("http://x", "s1", mapped)
    assert "sk-ABC123SECRETKEY000000000000" not in str(captured["payload"])


def test_add_payload_tool_args_redacted(monkeypatch):
    captured = _capture(monkeypatch)
    mapped = [
        {
            "sender_id": "ivan-coding",
            "role": "assistant",
            "timestamp": 2,
            "content": "run",
            "tool_calls": [
                {
                    "id": "t",
                    "type": "function",
                    "function": {
                        "name": "curl",
                        "arguments": "curl -H 'Authorization: Bearer sk-XYZSECRET99999999'",
                    },
                }
            ],
        }
    ]
    feed.feed_session("http://x", "s1", mapped)
    blob = str(captured["payload"])
    assert "sk-XYZSECRET99999999" not in blob
    assert "REDACTED" in blob  # 确实是被替换了，不是整条丢了


def test_redaction_does_not_mutate_caller_input(monkeypatch):
    _capture(monkeypatch)
    original = {
        "sender_id": "ivan-coding",
        "role": "assistant",
        "timestamp": 3,
        "content": "sk-ABC123SECRETKEY000000000000",
        "tool_calls": [
            {"id": "t", "type": "function", "function": {"name": "c", "arguments": "sk-XYZSECRET99999999"}}
        ],
    }
    feed.feed_session("http://x", "s1", [original])
    # 调用方的 dict 不被就地改写（否则重试/日志会拿到半脱敏对象）
    assert original["content"] == "sk-ABC123SECRETKEY000000000000"
    assert original["tool_calls"][0]["function"]["arguments"] == "sk-XYZSECRET99999999"


def test_batch_over_500_rejected(monkeypatch):
    _capture(monkeypatch)
    mapped = [{"sender_id": "a", "role": "user", "timestamp": i, "content": "x"} for i in range(501)]
    try:
        feed.feed_session("http://x", "s1", mapped)
    except ValueError as e:
        assert "500" in str(e)
    else:
        raise AssertionError("超过 DTO max_length=500 应被拒")
