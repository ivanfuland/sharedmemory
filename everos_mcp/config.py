# everos_mcp/config.py
"""Fail-fast config loader for everos_mcp.

规则(见任务简报,均为审查阻断项):
- 全部 SHADOW_*/EVEROS_* env 只在本模块读取;其他模块一律拿 Config 实例,不许直读 os.environ。
- 必需 env 缺任一 -> ConfigError(启动 fail-fast,不做部分配置降级运行)。
- 路径类 env 一律 Path(v).expanduser()(EnvironmentFile 不做 shell 展开,~ 必须代码侧展开)。
- everos_base/infinity_base 的 host 必须是 loopback(127.0.0.0/8 或字面量 localhost),
  防止 shadow 探针配置漂移到非本机地址。
- ledger_dir 必须是真实存在的目录、非 symlink、owner 是当前 uid。
- 代码内不含任何端口号/主目录字面量之类的拓扑默认值;必需项一律 fail-fast,不做隐式回退。
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_VALID_TRAFFIC_CLASSES = frozenset({"real", "synthetic_bench", "fault_test"})

_REQUIRED_ENV = (
    "EVEROS_MCP_PORT",
    "EVEROS_MCP_TOKEN",
    "EVEROS_BASE_URL",
    "EVEROS_AGENT_ID",
    "INFINITY_BASE",
    "SHADOW_LEDGER_DIR",
    "EVEROS_EMBED_MODEL",
    "EVEROS_RERANK_MODEL",
    "EVEROS_PIN_FILE",
    "EVEROS_INSTANCE_DIR",
    "INFINITY_CONTAINER",
)


class ConfigError(Exception):
    """Startup-time config validation error — must abort, never run partially configured."""


@dataclass(frozen=True)
class Config:
    port: int
    token: str
    everos_base: str
    agent_id: str
    infinity_base: str
    ledger_dir: Path
    expect_empty: bool
    embed_model: str
    rerank_model: str
    pin_file: Path
    instance_dir: Path
    infinity_container: str
    traffic_class: str
    fault: str | None


def _assert_loopback_host(url: str, env_name: str) -> None:
    hostname = urlparse(url).hostname
    if not hostname:
        raise ConfigError(f"{env_name} 缺少可解析的 host: {url!r}")
    if hostname == "localhost":
        return
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        raise ConfigError(f"{env_name} host 非 loopback: {hostname!r}") from None
    if not addr.is_loopback:
        raise ConfigError(f"{env_name} host 非 loopback: {hostname!r}")


def _assert_ledger_dir(path: Path) -> None:
    if path.is_symlink():
        raise ConfigError(f"SHADOW_LEDGER_DIR 不得是 symlink: {path}")
    if not path.is_dir():
        raise ConfigError(f"SHADOW_LEDGER_DIR 必须是真实存在的目录: {path}")
    if path.stat().st_uid != os.getuid():
        raise ConfigError(f"SHADOW_LEDGER_DIR owner 必须是当前 uid: {path}")


def load() -> Config:
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise ConfigError(f"缺少必需环境变量: {', '.join(missing)}")

    raw = {k: os.environ[k] for k in _REQUIRED_ENV}

    try:
        port = int(raw["EVEROS_MCP_PORT"])
    except ValueError:
        raise ConfigError(f"EVEROS_MCP_PORT 必须是整数: {raw['EVEROS_MCP_PORT']!r}") from None

    _assert_loopback_host(raw["EVEROS_BASE_URL"], "EVEROS_BASE_URL")
    _assert_loopback_host(raw["INFINITY_BASE"], "INFINITY_BASE")

    ledger_dir = Path(raw["SHADOW_LEDGER_DIR"]).expanduser()
    _assert_ledger_dir(ledger_dir)

    pin_file = Path(raw["EVEROS_PIN_FILE"]).expanduser()
    instance_dir = Path(raw["EVEROS_INSTANCE_DIR"]).expanduser()

    expect_empty = os.environ.get("EVEROS_MCP_EXPECT_EMPTY") == "1"
    traffic_class = os.environ.get("SHADOW_TRAFFIC_CLASS") or "real"
    if traffic_class not in _VALID_TRAFFIC_CLASSES:
        allowed = ", ".join(sorted(_VALID_TRAFFIC_CLASSES))
        raise ConfigError(
            f"SHADOW_TRAFFIC_CLASS 非法值: {traffic_class!r}，允许值: {{{allowed}}}"
        )
    fault = os.environ.get("SHADOW_FAULT") or None

    return Config(
        port=port,
        token=raw["EVEROS_MCP_TOKEN"],
        everos_base=raw["EVEROS_BASE_URL"],
        agent_id=raw["EVEROS_AGENT_ID"],
        infinity_base=raw["INFINITY_BASE"],
        ledger_dir=ledger_dir,
        expect_empty=expect_empty,
        embed_model=raw["EVEROS_EMBED_MODEL"],
        rerank_model=raw["EVEROS_RERANK_MODEL"],
        pin_file=pin_file,
        instance_dir=instance_dir,
        infinity_container=raw["INFINITY_CONTAINER"],
        traffic_class=traffic_class,
        fault=fault,
    )
