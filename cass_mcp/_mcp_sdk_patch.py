"""mcp SDK 1.28.1 `Server._handle_message` 运行时补丁(python-sdk#2064)。

**Canonical 源 = `everos_mcp/server.py` 的同名补丁块**(逐字核对/对抗审通过的
原版,含完整背景长注释)。本文件是它面向 cass_mcp 的忠实拷贝——两份必须
同升同删:上游修掉 #2064 或本仓升级 mcp 版本时,一起处理,不允许单边漂移。

为什么打补丁(极简版,详见 canonical 源):客户端断连瞬间,`_handle_message`
往已关闭的写流回报错误会抛未捕获 `ClosedResourceError`,把"通知失败"级别的
次生异常升级成服务瘫痪。上游修复(PR #2072)只存在于 mcp 2.x 预发布线,而
fastmcp 3.4.2 钉死 `mcp<2.0`——无可用上游发行版,只能运行时最小替换。

版本钉死断言:仅在 mcp == 补丁逐字核对过的版本时生效;版本不符 **跳过打
补丁**(不是警告后照打)+ CRITICAL 日志,不阻断启动——版本不符 = 核对已
过期,交人工复核(2026-07-18 everos-mcp 实装期定的纪律,cass_mcp 沿用)。
"""
from __future__ import annotations

import importlib.metadata
import logging
import warnings

import anyio
import mcp.server.lowlevel.server as _mcp_lowlevel_server

_LOG = logging.getLogger("cass_mcp.mcp_sdk_patch")

MCP_HANDLE_MESSAGE_PATCH_VERIFIED_VERSION = "1.28.1"


async def _patched_handle_message(
    self,
    message,
    session,
    lifespan_context,
    raise_exceptions: bool = False,
):
    """`mcp.server.lowlevel.server.Server._handle_message` 的运行时替换版——
    与上游原实现逐字一致,唯一改动是给 `send_log_message` 套
    try/except(anyio.ClosedResourceError, anyio.BrokenResourceError),
    对应官方未合并 PR #2072 的修复逻辑。签名/行为之外的任何改写都不允许。"""
    mod = _mcp_lowlevel_server
    with warnings.catch_warnings(record=True) as w:
        match message:
            case mod.RequestResponder(request=mod.types.ClientRequest(root=req)) as responder:
                with responder:
                    await self._handle_request(message, req, session, lifespan_context, raise_exceptions)
            case mod.types.ClientNotification(root=notify):
                await self._handle_notification(notify)
            case Exception():
                mod.logger.error(f"Received exception from stream: {message}")
                try:
                    await session.send_log_message(
                        level="error",
                        data="Internal Server Error",
                        logger="mcp.server.exception_handler",
                    )
                except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                    # 客户端已断连,回报失败是预期内场景——这正是本补丁
                    # 存在的理由(#2064 / PR #2072,详见 canonical 源注释)。
                    mod.logger.debug("Could not send error log: client disconnected")
                if raise_exceptions:
                    raise message

        for warning in w:
            mod.logger.info("Warning: %s: %s", warning.category.__name__, warning.message)


def apply_mcp_handle_message_patch() -> None:
    """版本不符 -> 跳过打补丁 + CRITICAL 日志,不阻断启动(语义与 canonical
    源逐字一致,理由见其文档字符串)。"""
    try:
        installed = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover — 环境损坏,交给别处报错
        installed = "unknown"
    if installed != MCP_HANDLE_MESSAGE_PATCH_VERIFIED_VERSION:
        _LOG.critical(
            f"cass_mcp: mcp SDK 版本为 {installed!r},与本补丁逐字核对过的版本 "
            f"{MCP_HANDLE_MESSAGE_PATCH_VERIFIED_VERSION!r} 不同——**跳过** "
            "_handle_message 补丁(版本不符 = 核对已过期,交人工复核;canonical "
            "源见 everos_mcp/server.py,两份同升同删)。服务仍会启动,但客户端"
            "断连场景下 #2064 的未捕获 ClosedResourceError 不再有本补丁兜底。"
        )
        return
    _mcp_lowlevel_server.Server._handle_message = _patched_handle_message
