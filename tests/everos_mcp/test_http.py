"""http.post_json 的出站唯一通道契约测试(见任务简报)。

固定纪律:
- 本地 stub 用 `http.server.ThreadingHTTPServer` 绑 `127.0.0.1` + 端口 0
  (ephemeral,PUBLIC repo 拓扑字面量白名单例外)。
- redirect 拒绝必须在 opener 层验证:第二个 stub 的请求计数必须为 0,不是
  只断言异常类型(否则包装层"检查了但没挡住"的坏实现也能骗过测试)。
- 非 loopback 拒发用 RFC 5737 TEST-NET 文档保留段(203.0.113.0/24),不是
  真实可达地址,且请求根本不会发出去。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from everos_mcp import http


class _Counter:
    def __init__(self):
        self.lock = threading.Lock()
        self.n = 0

    def bump(self) -> int:
        with self.lock:
            self.n += 1
            return self.n


class _Stub:
    """通用本地 HTTP stub,行为由 `handler_fn(request_body: dict) -> (status, body_bytes|None, headers)` 决定。

    GET 请求(无 body 可解析)同样路由到 `handler_fn`,传入空 dict——`get_json`
    测试与 `post_json` 测试复用同一个 stub 类。"""

    def __init__(self, handler_fn):
        self.counter = _Counter()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _respond(self, parsed):
                status, body, extra_headers = handler_fn(parsed)
                self.send_response(status)
                for k, v in extra_headers.items():
                    self.send_header(k, v)
                if body is not None:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body is not None:
                    self.wfile.write(body)

            def do_POST(self):
                outer.counter.bump()
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    parsed = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = {}
                self._respond(parsed)

            def do_GET(self):
                outer.counter.bump()
                self._respond({})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _make_stub(handler_fn) -> _Stub:
    return _Stub(handler_fn).start()


# ======================================================================
# redirect 拒绝(opener 级):第二个 stub 必须零请求
# ======================================================================

def test_redirect_refused_second_stub_receives_zero_requests():
    second = _make_stub(lambda body: (200, json.dumps({"ok": True}).encode(), {"Content-Type": "application/json"}))
    try:
        first = _make_stub(
            lambda body: (302, b"", {"Location": second.base_url + "/dest"})
        )
        try:
            with pytest.raises(http.RedirectRefused):
                http.post_json(first.base_url + "/src", {"q": "x"}, timeout=5)
        finally:
            first.stop()
        assert second.counter.n == 0
    finally:
        second.stop()


# ======================================================================
# 非 loopback host 拒发(发送前断言,不发出任何字节)
# ======================================================================

@pytest.mark.parametrize("bad_url", [
    "http://203.0.113.5:80/x",
    "http://example.invalid/x",
    "http://198.51.100.7:8080/x",
])
def test_non_loopback_url_refused_before_send(bad_url):
    with pytest.raises(ValueError):
        http.post_json(bad_url, {"q": "x"}, timeout=1)


def test_loopback_localhost_literal_accepted():
    stub = _make_stub(lambda body: (200, json.dumps({"echo": body}).encode(), {"Content-Type": "application/json"}))
    try:
        # server bound to 127.0.0.1 but we address it via the literal "localhost" host
        host, port = stub.server.server_address
        url = f"http://localhost:{port}/x"
        result = http.post_json(url, {"q": "y"}, timeout=5)
        assert result == {"echo": {"q": "y"}}
    finally:
        stub.stop()


# ======================================================================
# timeout 生效
# ======================================================================

def test_timeout_raises():
    def slow_handler(body):
        import time
        time.sleep(2)
        return (200, b"{}", {"Content-Type": "application/json"})

    stub = _make_stub(slow_handler)
    try:
        with pytest.raises(TimeoutError):
            http.post_json(stub.base_url + "/slow", {"q": "z"}, timeout=0.2)
    finally:
        stub.stop()


# ======================================================================
# BadJson:非法 UTF-8 字节流 / 合法 UTF-8 但非法 JSON
# ======================================================================

def test_bad_json_invalid_utf8_bytes():
    stub = _make_stub(lambda body: (200, b"\xff\xfe\x00\x01", {"Content-Type": "application/json"}))
    try:
        with pytest.raises(http.BadJson):
            http.post_json(stub.base_url + "/x", {"q": "x"}, timeout=5)
    finally:
        stub.stop()


def test_bad_json_valid_utf8_invalid_json():
    stub = _make_stub(lambda body: (200, "不是 JSON 的纯文字回复".encode("utf-8"), {"Content-Type": "application/json"}))
    try:
        with pytest.raises(http.BadJson):
            http.post_json(stub.base_url + "/x", {"q": "x"}, timeout=5)
    finally:
        stub.stop()


# ======================================================================
# happy path:正常 POST + JSON 响应
# ======================================================================

def test_happy_path_returns_parsed_json():
    stub = _make_stub(lambda body: (200, json.dumps({"received": body}).encode("utf-8"),
                                     {"Content-Type": "application/json"}))
    try:
        result = http.post_json(stub.base_url + "/ok", {"a": 1, "b": "two"}, timeout=5)
        assert result == {"received": {"a": 1, "b": "two"}}
        assert stub.counter.n == 1
    finally:
        stub.stop()


# ======================================================================
# 4xx/5xx 原样上抛 HTTPError(调用方读 body)
# ======================================================================

def test_http_error_propagates_with_body_readable():
    from urllib.error import HTTPError

    stub = _make_stub(lambda body: (500, b"internal stub error", {"Content-Type": "text/plain"}))
    try:
        with pytest.raises(HTTPError) as exc_info:
            http.post_json(stub.base_url + "/boom", {"q": "x"}, timeout=5)
        assert exc_info.value.code == 500
        assert exc_info.value.read() == b"internal stub error"
    finally:
        stub.stop()


# ======================================================================
# P1c: get_json —— 同一出站通道(loopback + 拒绝 redirect),供 `/models` 探针用
# ======================================================================

def test_get_json_happy_path():
    stub = _make_stub(lambda body: (200, json.dumps({"data": [{"id": "m1"}]}).encode("utf-8"),
                                     {"Content-Type": "application/json"}))
    try:
        result = http.get_json(stub.base_url + "/models", timeout=5)
        assert result == {"data": [{"id": "m1"}]}
        assert stub.counter.n == 1
    finally:
        stub.stop()


def test_get_json_redirect_refused_second_stub_receives_zero_requests():
    second = _make_stub(lambda body: (200, json.dumps({"ok": True}).encode(), {"Content-Type": "application/json"}))
    try:
        first = _make_stub(lambda body: (302, b"", {"Location": second.base_url + "/dest"}))
        try:
            with pytest.raises(http.RedirectRefused):
                http.get_json(first.base_url + "/models", timeout=5)
        finally:
            first.stop()
        assert second.counter.n == 0
    finally:
        second.stop()


@pytest.mark.parametrize("bad_url", [
    "http://203.0.113.5:80/models",
    "http://example.invalid/models",
])
def test_get_json_non_loopback_url_refused_before_send(bad_url):
    with pytest.raises(ValueError):
        http.get_json(bad_url, timeout=1)


def test_get_json_bad_json_valid_utf8_invalid_json():
    stub = _make_stub(lambda body: (200, "不是 JSON 的纯文字回复".encode("utf-8"), {"Content-Type": "application/json"}))
    try:
        with pytest.raises(http.BadJson):
            http.get_json(stub.base_url + "/models", timeout=5)
    finally:
        stub.stop()


# ======================================================================
# P2b: 响应体读取硬顶 —— 超限 -> ResponseTooLarge,不回显内容
# ======================================================================

def test_post_json_response_too_large_refused(monkeypatch):
    monkeypatch.setattr(http, "MAX_RESPONSE_BYTES", 16)
    oversized = b"x" * 64
    stub = _make_stub(lambda body: (200, oversized, {"Content-Type": "application/json"}))
    try:
        with pytest.raises(http.ResponseTooLarge) as exc_info:
            http.post_json(stub.base_url + "/big", {"q": "x"}, timeout=5)
        assert b"x" * 64 not in str(exc_info.value).encode("utf-8", errors="ignore")
    finally:
        stub.stop()


def test_get_json_response_too_large_refused(monkeypatch):
    monkeypatch.setattr(http, "MAX_RESPONSE_BYTES", 16)
    oversized = b"y" * 64
    stub = _make_stub(lambda body: (200, oversized, {"Content-Type": "application/json"}))
    try:
        with pytest.raises(http.ResponseTooLarge):
            http.get_json(stub.base_url + "/big", timeout=5)
    finally:
        stub.stop()


def test_response_within_cap_still_succeeds(monkeypatch):
    monkeypatch.setattr(http, "MAX_RESPONSE_BYTES", 4096)
    stub = _make_stub(lambda body: (200, json.dumps({"ok": True}).encode("utf-8"),
                                     {"Content-Type": "application/json"}))
    try:
        result = http.post_json(stub.base_url + "/small", {"q": "x"}, timeout=5)
        assert result == {"ok": True}
    finally:
        stub.stop()
