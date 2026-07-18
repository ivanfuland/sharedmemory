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


# ---------------------------------------------------------------- 生产入口钉死
def test_production_entrypoint_runs_stateless_http(monkeypatch):
    # codex R2 P1:测试必须执行真实的 `python -m cass_mcp.server` 入口路径,
    # 钉住 mcp.run 收到 stateless_http=True——删掉生产参数本测试即红。
    import runpy
    import fastmcp

    monkeypatch.setenv("CASS_MCP_BEARER", "synthetic-test-bearer")
    captured: dict = {}

    def fake_run(self, *args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(fastmcp.FastMCP, "run", fake_run)
    runpy.run_module("cass_mcp.server", run_name="__main__")
    assert captured.get("stateless_http") is True
    assert captured.get("transport") == "http"
    assert captured.get("host") == "127.0.0.1"


# ---------------------------------------------------------------- stateless HTTP 回归
@pytest.fixture()
def cass_app(monkeypatch):
    monkeypatch.setenv("CASS_MCP_BEARER", "synthetic-test-bearer")
    monkeypatch.setenv("CASS_MCP_TOKEN_ID", "hub")  # 显式钉死,防外部环境非 hub 值假红(codex R3 nit)
    import importlib
    import cass_mcp.server as srv
    importlib.reload(srv)  # 让模块级 bearer/token_id 读到本测试的合成值
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
    asyncio.run(run_httpx())


def test_stateless_real_tool_roundtrip_with_audit(cass_app, monkeypatch, tmp_path):
    # codex R2 P1 后半:正确 bearer 必须完成**成功的 MCP 初始化 + 真实工具调用**
    # (不能只判非 401),且真实审计路径写出 token_id="hub"(get_access_token 在
    # stateless 模式下工作)。工具语义层面 cass 二进制不在测试环境,cass_triage
    # 返回 not_ready 结构也算完整往返——断言的是 stateless 模式下
    # 协议握手/鉴权上下文/工具执行/审计 四层全通,不打任何桩。
    import json as _json
    import httpx
    from fastmcp.client import Client
    from fastmcp.client.transports import StreamableHttpTransport

    audit_file = tmp_path / "audit.log"
    monkeypatch.setenv("CASS_MCP_AUDIT", str(audit_file))

    app = cass_app.mcp.http_app(stateless_http=True)

    async def run():
        async with app.router.lifespan_context(app):
            transport = StreamableHttpTransport(
                "http://cass-test.internal/mcp",
                headers={"Authorization": "Bearer synthetic-test-bearer"},
                httpx_client_factory=lambda **kw: httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), **{k: v for k, v in kw.items() if k != "transport"}
                ),
            )
            async with Client(transport) as c:      # 进得来 = initialize 成功
                r = await c.call_tool("cass_triage", {})
                assert r is not None                # 工具真实执行并返回

    asyncio.run(run())
    recs = [_json.loads(l) for l in audit_file.read_text().splitlines()]
    assert recs, "工具调用未写审计"
    assert recs[-1]["tool"] == "cass_triage"
    assert recs[-1]["token_id"] == "hub"            # bearer→client_id 审计链在 stateless 下完好
