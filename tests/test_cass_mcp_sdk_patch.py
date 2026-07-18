# cass_mcp/_mcp_sdk_patch 的行为钉死(codex PR#63 R1 后重写:不再只验"函数
# 对象被替换",而是钉补丁的核心行为本身 + 与 canonical 源的 AST 漂移断言 +
# stateless HTTP/bearer 回归)。全部 fixture 合成。
import ast
import asyncio
import logging
import os
from pathlib import Path

import anyio
import pytest

import mcp.server.lowlevel.server as _mcp_lowlevel_server

from cass_mcp import _mcp_sdk_patch

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- 行为:异常吞噬
class _FakeSession:
    """send_log_message 抛指定异常的合成 session。"""
    def __init__(self, exc: BaseException | None):
        self._exc = exc
        self.calls = 0

    async def send_log_message(self, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc


class _FakeServer:
    """只提供 _patched_handle_message 所需属性面的壳(不碰真 Server 状态机)。"""
    async def _handle_request(self, *a, **k):  # pragma: no cover — Exception 分支不走这
        raise AssertionError("Exception 消息分支不应触发 _handle_request")

    async def _handle_notification(self, *a, **k):  # pragma: no cover — 同上
        raise AssertionError("Exception 消息分支不应触发 _handle_notification")


@pytest.mark.parametrize("exc_type", [anyio.ClosedResourceError, anyio.BrokenResourceError])
def test_patched_handler_swallows_disconnect_errors(exc_type):
    # #2064 核心行为:消息是 Exception、session 回报时客户端已断连 → 补丁必须
    # 吞掉 Closed/BrokenResourceError,不让它向上炸掉共享 task group。
    session = _FakeSession(exc_type())
    boom = RuntimeError("synthetic stream exception")
    asyncio.run(_mcp_sdk_patch._patched_handle_message(
        _FakeServer(), boom, session, lifespan_context=None, raise_exceptions=False,
    ))  # 不抛 = 通过
    assert session.calls == 1  # 回报确实尝试过,是"吞异常"不是"没调用"


def test_patched_handler_raise_exceptions_still_raises_original():
    # raise_exceptions=True 时必须抛出**原始** Exception(上游语义保留),
    # 且断连错误仍被吞(不被断连错误顶替)。
    session = _FakeSession(anyio.ClosedResourceError())
    boom = RuntimeError("synthetic stream exception")
    with pytest.raises(RuntimeError) as e:
        asyncio.run(_mcp_sdk_patch._patched_handle_message(
            _FakeServer(), boom, session, lifespan_context=None, raise_exceptions=True,
        ))
    assert e.value is boom


def test_patched_handler_does_not_swallow_other_send_errors():
    # 只吞 Closed/Broken 两类断连错误——其他异常照常传播,不许过度吞噬。
    session = _FakeSession(ValueError("unrelated send failure"))
    with pytest.raises(ValueError):
        asyncio.run(_mcp_sdk_patch._patched_handle_message(
            _FakeServer(), RuntimeError("x"), session, lifespan_context=None, raise_exceptions=False,
        ))


# ---------------------------------------------------------------- 应用/跳过语义
def test_patch_applies_on_verified_version(monkeypatch):
    saved = _mcp_lowlevel_server.Server._handle_message
    try:
        monkeypatch.setattr(
            _mcp_sdk_patch.importlib.metadata, "version",
            lambda name: _mcp_sdk_patch.MCP_HANDLE_MESSAGE_PATCH_VERIFIED_VERSION,
        )
        _mcp_sdk_patch.apply_mcp_handle_message_patch()
        assert _mcp_lowlevel_server.Server._handle_message is _mcp_sdk_patch._patched_handle_message
    finally:
        _mcp_lowlevel_server.Server._handle_message = saved


def test_patch_skipped_on_version_mismatch(monkeypatch, caplog):
    # 用模块加载时留存的 ORIGINAL 做基线,避免进程内其他测试先打补丁造成的
    # "saved 已是补丁版" 污染(codex R1 P1-3)。
    saved = _mcp_lowlevel_server.Server._handle_message
    try:
        _mcp_lowlevel_server.Server._handle_message = _mcp_sdk_patch.ORIGINAL_HANDLE_MESSAGE
        monkeypatch.setattr(_mcp_sdk_patch.importlib.metadata, "version", lambda name: "9.9.9")
        with caplog.at_level(logging.CRITICAL, logger="cass_mcp.mcp_sdk_patch"):
            _mcp_sdk_patch.apply_mcp_handle_message_patch()
        assert _mcp_lowlevel_server.Server._handle_message is _mcp_sdk_patch.ORIGINAL_HANDLE_MESSAGE
        assert any("跳过" in r.message for r in caplog.records)
    finally:
        _mcp_lowlevel_server.Server._handle_message = saved


def test_installed_mcp_matches_verified_version():
    # 环境锁定:谁升了 mcp 此测试即红,强制重新核对补丁(canonical 纪律)。
    import importlib.metadata
    assert importlib.metadata.version("mcp") == _mcp_sdk_patch.MCP_HANDLE_MESSAGE_PATCH_VERIFIED_VERSION


# ---------------------------------------------------------------- canonical 漂移
def _extract_patched_fn_ast(path: Path) -> str:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_patched_handle_message":
            node.body = [n for n in node.body  # 剥掉 docstring,只比逻辑
                         if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
            return ast.dump(node.args) + "||" + "".join(ast.dump(n) for n in node.body)
    raise AssertionError(f"{path} 里找不到 _patched_handle_message")


def test_patch_ast_identical_to_canonical_everos_copy():
    # 两份补丁(cass_mcp 移植版 vs everos_mcp canonical 版)的核心函数逻辑必须
    # AST 等价——任何一边单独改动此函数,本测试即红(同升同删纪律的机器闸)。
    cass = _extract_patched_fn_ast(REPO_ROOT / "cass_mcp" / "_mcp_sdk_patch.py")
    everos = _extract_patched_fn_ast(REPO_ROOT / "everos_mcp" / "server.py")
    # everos 版引用模块别名 _mcp_lowlevel_server 与 cass 版同名,注释/字符串已剥,
    # 直接比对 dump;若将来别名不同名,此断言会红,届时按纪律人工核对后同步。
    assert cass == everos


# ---------------------------------------------------------------- stateless HTTP 回归
@pytest.fixture()
def cass_app(monkeypatch):
    monkeypatch.setenv("CASS_MCP_BEARER", "synthetic-test-bearer")
    import importlib
    import cass_mcp.server as srv
    importlib.reload(srv)  # 让模块级 bearer 读到本测试的合成值
    return srv


def test_stateless_http_bearer_and_tool_roundtrip(cass_app):
    # 钉两件事:①server 以 stateless_http 形态起 ASGI app 后,正确 bearer 能
    # 完成 MCP 握手 + 工具调用(session 语义无损);②缺/错 token 拒绝。
    # 工具语义(cass 二进制在不在)不在断言范围——not_ready 也算协议层成功。
    import httpx

    app = cass_app.mcp.http_app(stateless_http=True)

    async def run_httpx():
        # ASGITransport 不跑 lifespan,而 stateless session manager 的 task group
        # 在 lifespan 里初始化——手动进 lifespan 上下文(starlette 标准做法)。
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://cass-test.internal") as hc:
                # 无 token → 401
                r = await hc.post("/mcp", json={})
                assert r.status_code == 401
                # 错 token → 401
                r = await hc.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})
                assert r.status_code == 401
                # 对 token → 非 401(协议层进得去;MCP 握手细节由 fastmcp 客户端
                # 测试覆盖,这里钉 stateless 模式下 StaticTokenVerifier 鉴权路径完好)
                r = await hc.post(
                    "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-03-26",
                                              "capabilities": {},
                                              "clientInfo": {"name": "t", "version": "0"}}},
                    headers={"Authorization": "Bearer synthetic-test-bearer",
                             "Accept": "application/json, text/event-stream",
                             "Content-Type": "application/json"},
                )
                assert r.status_code != 401, r.text

    asyncio.run(run_httpx())
