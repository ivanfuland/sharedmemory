# everos_mcp/http.py
"""everos_mcp 的唯一出站通道。

规则(见任务简报,均为审查阻断项):
- 拒绝 redirect 必须在 opener 层做:覆写 `HTTPRedirectHandler.redirect_request`
  直接 raise,而不是靠"检查响应状态码再决定要不要跟随"这种包装——后者挡不住
  urllib 自己已经在内部发出的第二个请求(危险发生在 opener 决定 follow 的那一
  刻,不是在调用方看到结果的那一刻)。
- 发请求前断言 url host 是 loopback(127.0.0.0/8 或字面量 localhost),防止
  shadow 探针的出站目标漂移到非本机地址。
- 响应体 JSON decode 失败(非法 UTF-8 字节流 / 合法 UTF-8 但非法 JSON)在本层
  统一转 `BadJson`,不留作裸 `UnicodeDecodeError`/`json.JSONDecodeError`——上游
  调用方(upstream.py)只需要认一种"响应解析失败"异常。
- 除以上两类断言外,不吞任何异常:HTTPError(4xx/5xx)原样向上抛,由调用方
  决定如何读 body / 映射错误码。
"""
from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RedirectRefused(Exception):
    """上游返回 30x 重定向——拒绝跟随,重定向目标请求永不发出。"""


class BadJson(Exception):
    """响应体不是合法 UTF-8,或是合法 UTF-8 但不是合法 JSON。"""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 (urllib 接口名)
        raise RedirectRefused(f"拒绝跟随重定向: HTTP {code} -> {newurl!r}")


_opener = build_opener(_NoRedirect())


def _assert_loopback_host(url: str) -> None:
    hostname = urlparse(url).hostname
    if not hostname:
        raise ValueError(f"post_json: url 缺少可解析的 host: {url!r}")
    if hostname == "localhost":
        return
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        raise ValueError(f"post_json: url host 非 loopback: {hostname!r}") from None
    if not addr.is_loopback:
        raise ValueError(f"post_json: url host 非 loopback: {hostname!r}")


def post_json(url: str, payload: dict, timeout: float) -> dict:
    """POST JSON 到 loopback url,经拒绝重定向的 opener 发出。

    - 发送前断言 host 为 loopback,否则 `ValueError`(拒发,不发出任何字节)。
    - 30x 重定向 -> `RedirectRefused`(第二个请求永不发出)。
    - 4xx/5xx -> `urllib.error.HTTPError` 原样上抛(调用方自行读 body)。
    - 响应体解析失败(非法 UTF-8 / 非法 JSON) -> `BadJson`。
    """
    _assert_loopback_host(url)
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with _opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BadJson(f"post_json {url}: 响应非合法 UTF-8: {e}") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise BadJson(f"post_json {url}: 响应非合法 JSON: {e}") from e
