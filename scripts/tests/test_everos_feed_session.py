"""feeder 纯逻辑件测试。fixture 全合成(PUBLIC 仓铁律)。"""
import httpx
import pytest

from scripts.everos_feed_session import _AddCountingHttpx, _is_pre_add_transient


class _FakeResp:
    def __init__(self, code):
        self.status_code = code


class _FakeHttpx:
    """替身 real httpx:按预置队列返回响应或抛异常。"""
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(url)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item)


def test_counting_hook_counts_only_successful_add():
    fake = _FakeHttpx([200, 422, 200])
    probe = _AddCountingHttpx(fake)
    assert probe.post("http://x/api/v1/memory/add").status_code == 200
    assert probe.add_ok == 1
    probe.post("http://x/api/v1/memory/add")          # 422:不计数
    assert probe.add_ok == 1
    probe.post("http://x/api/v1/memory/flush")        # flush:不计数
    assert probe.add_ok == 1


def test_counting_hook_passes_exceptions_through():
    fake = _FakeHttpx([httpx.ConnectError("boom")])
    probe = _AddCountingHttpx(fake)
    with pytest.raises(httpx.ConnectError):
        probe.post("http://x/api/v1/memory/add")
    assert probe.add_ok == 0


def _status_error(code):
    req = httpx.Request("POST", "http://x/api/v1/memory/add")
    return httpx.HTTPStatusError("err", request=req, response=httpx.Response(code, request=req))


@pytest.mark.parametrize("exc,add_ok,expected", [
    (httpx.ConnectError("x"), 0, True),      # 连接类:请求没到实例,零副作用 → 可退避
    (httpx.ConnectTimeout("x"), 0, True),
    (_status_error(422), 0, True),           # 422 busy:实例收到并拒绝,零副作用 → 可退避(M1b 实证)
    (_status_error(500), 0, False),          # 5xx:语义不明,可能已部分处理 → 不重试
    (httpx.ReadTimeout("x"), 0, False),      # 响应缺失:请求可能已落地 → 不重试
    (httpx.ConnectError("x"), 1, False),     # 已有 /add 落地:重放整个 run_session 会重复喂前缀 → 一律不重试
    (_status_error(422), 2, False),
])
def test_is_pre_add_transient(exc, add_ok, expected):
    assert _is_pre_add_transient(exc, add_ok) is expected


@pytest.mark.parametrize("exc,add_ok,expected", [
    (httpx.ConnectError("x"), 0, True),      # 请求没到达 → 确定零副作用
    (httpx.ConnectTimeout("x"), 0, True),
    (_status_error(422), 0, True),           # 4xx = 服务端收到并拒绝 → 确定零副作用
    (_status_error(429), 0, True),           # 预算/限流拒(spec §5:首个 /add 前预算拒 → 回 pending)
    (_status_error(402), 0, True),
    (_status_error(500), 0, False),          # 5xx:可能已部分处理 → 不能按零副作用回 pending
    (httpx.ReadTimeout("x"), 0, False),      # 请求可能已落地 → 不能
    (_status_error(422), 1, False),          # 已有 /add 落地 → 一律不是零副作用
])
def test_is_no_side_effect(exc, add_ok, expected):
    from scripts.everos_feed_session import _is_no_side_effect
    assert _is_no_side_effect(exc, add_ok) is expected
