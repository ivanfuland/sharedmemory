# cass_mcp/_mcp_sdk_patch 的行为钉死:版本匹配才打补丁,不符则跳过+CRITICAL。
# canonical 源在 everos_mcp/server.py(两份同升同删);本测试保证 cass_mcp 侧
# 拷贝的应用逻辑不漂移。
import logging

import mcp.server.lowlevel.server as _mcp_lowlevel_server

from cass_mcp import _mcp_sdk_patch


def test_patch_applies_on_verified_version(monkeypatch):
    original = _mcp_lowlevel_server.Server._handle_message
    try:
        monkeypatch.setattr(
            _mcp_sdk_patch.importlib.metadata, "version",
            lambda name: _mcp_sdk_patch.MCP_HANDLE_MESSAGE_PATCH_VERIFIED_VERSION,
        )
        _mcp_sdk_patch.apply_mcp_handle_message_patch()
        assert _mcp_lowlevel_server.Server._handle_message is _mcp_sdk_patch._patched_handle_message
    finally:
        _mcp_lowlevel_server.Server._handle_message = original


def test_patch_skipped_on_version_mismatch(monkeypatch, caplog):
    original = _mcp_lowlevel_server.Server._handle_message
    try:
        # 先恢复到未打补丁状态,再用不符版本尝试
        _mcp_lowlevel_server.Server._handle_message = original
        monkeypatch.setattr(
            _mcp_sdk_patch.importlib.metadata, "version", lambda name: "9.9.9",
        )
        with caplog.at_level(logging.CRITICAL, logger="cass_mcp.mcp_sdk_patch"):
            _mcp_sdk_patch.apply_mcp_handle_message_patch()
        assert _mcp_lowlevel_server.Server._handle_message is original  # 未被替换
        assert any("跳过" in r.message for r in caplog.records)
    finally:
        _mcp_lowlevel_server.Server._handle_message = original


def test_installed_mcp_matches_verified_version():
    # 锁定环境断言:uv.lock 里的 mcp 就是补丁核对过的版本——若此测试挂了,
    # 说明有人升了 mcp,必须重新核对补丁(canonical 源纪律)。
    import importlib.metadata
    assert importlib.metadata.version("mcp") == _mcp_sdk_patch.MCP_HANDLE_MESSAGE_PATCH_VERIFIED_VERSION
