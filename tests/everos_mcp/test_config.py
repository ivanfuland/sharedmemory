"""config.load() 的 fail-fast 契约测试。

固定纪律(见任务简报):
- 全部 fixture 用 tmp_path / 合成假值(端口"1"、token"test-token"),
  绝不硬编码真实端口/路径/凭证。
- SHADOW_*/EVEROS_*/INFINITY_* env 只经 config.py 读取——测试里只通过
  monkeypatch 注入,不直接读 os.environ 断言实现细节。
"""
import os

import pytest

from everos_mcp import config

_REQUIRED_KEYS = (
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


def _env(ledger_dir, everos_base="http://127.0.0.1:1", infinity_base="http://127.0.0.1:1"):
    return {
        "EVEROS_MCP_PORT": "1",
        "EVEROS_MCP_TOKEN": "test-token",
        "EVEROS_BASE_URL": everos_base,
        "EVEROS_AGENT_ID": "test-agent",
        "INFINITY_BASE": infinity_base,
        "SHADOW_LEDGER_DIR": str(ledger_dir),
        "EVEROS_EMBED_MODEL": "test-embed-model",
        "EVEROS_RERANK_MODEL": "test-rerank-model",
        "EVEROS_PIN_FILE": str(ledger_dir / "pin.json"),
        "EVEROS_INSTANCE_DIR": str(ledger_dir / "instance"),
        "INFINITY_CONTAINER": "test-container",
    }


def _apply_env(monkeypatch, env):
    # 先清掉当前进程里可能残留的同前缀 env,避免宿主机真实值泄漏进测试
    for k in list(os.environ):
        if k.startswith(("EVEROS_", "SHADOW_", "INFINITY_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_happy_path_all_required_env(tmp_path, monkeypatch):
    _apply_env(monkeypatch, _env(tmp_path))
    cfg = config.load()
    assert cfg.port == 1
    assert cfg.token == "test-token"
    assert cfg.everos_base == "http://127.0.0.1:1"
    assert cfg.agent_id == "test-agent"
    assert cfg.infinity_base == "http://127.0.0.1:1"
    assert cfg.ledger_dir == tmp_path
    assert cfg.embed_model == "test-embed-model"
    assert cfg.rerank_model == "test-rerank-model"
    assert cfg.pin_file == tmp_path / "pin.json"
    assert cfg.instance_dir == tmp_path / "instance"
    assert cfg.infinity_container == "test-container"
    # 可选 env 缺省值
    assert cfg.expect_empty is False
    assert cfg.traffic_class == "real"
    assert cfg.fault is None


@pytest.mark.parametrize("missing_key", _REQUIRED_KEYS)
def test_missing_required_env_fails_fast(tmp_path, monkeypatch, missing_key):
    env = _env(tmp_path)
    del env[missing_key]
    _apply_env(monkeypatch, env)
    with pytest.raises(config.ConfigError):
        config.load()


@pytest.mark.parametrize("bad_host", ["http://example.invalid:1", "http://203.0.113.5:1", "http://198.51.100.7:1"])
def test_non_loopback_everos_base_rejected(tmp_path, monkeypatch, bad_host):
    _apply_env(monkeypatch, _env(tmp_path, everos_base=bad_host))
    with pytest.raises(config.ConfigError):
        config.load()


@pytest.mark.parametrize("bad_host", ["http://example.invalid:1", "http://203.0.113.5:1"])
def test_non_loopback_infinity_base_rejected(tmp_path, monkeypatch, bad_host):
    _apply_env(monkeypatch, _env(tmp_path, infinity_base=bad_host))
    with pytest.raises(config.ConfigError):
        config.load()


def test_loopback_hostname_localhost_accepted(tmp_path, monkeypatch):
    _apply_env(
        monkeypatch,
        _env(tmp_path, everos_base="http://localhost:1", infinity_base="http://localhost:1"),
    )
    cfg = config.load()
    assert cfg.everos_base == "http://localhost:1"
    assert cfg.infinity_base == "http://localhost:1"


def test_symlinked_ledger_dir_rejected(tmp_path, monkeypatch):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    _apply_env(monkeypatch, _env(link_dir))
    with pytest.raises(config.ConfigError):
        config.load()


def test_missing_ledger_dir_rejected(tmp_path, monkeypatch):
    absent = tmp_path / "does-not-exist"
    _apply_env(monkeypatch, _env(absent))
    with pytest.raises(config.ConfigError):
        config.load()


def test_ledger_dir_expanduser(tmp_path, monkeypatch):
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    ledger = home_dir / "ledger"
    ledger.mkdir()
    env = _env(ledger)
    env["SHADOW_LEDGER_DIR"] = "~/ledger"
    _apply_env(monkeypatch, env)
    cfg = config.load()
    assert cfg.ledger_dir == ledger


def test_pin_file_and_instance_dir_expanduser(tmp_path, monkeypatch):
    home_dir = tmp_path / "home2"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    ledger = tmp_path / "ledger2"
    ledger.mkdir()
    env = _env(ledger)
    env["EVEROS_PIN_FILE"] = "~/pin.json"
    env["EVEROS_INSTANCE_DIR"] = "~/instance"
    _apply_env(monkeypatch, env)
    cfg = config.load()
    assert cfg.pin_file == home_dir / "pin.json"
    assert cfg.instance_dir == home_dir / "instance"


def test_expect_empty_true_when_set(tmp_path, monkeypatch):
    env = _env(tmp_path)
    env["EVEROS_MCP_EXPECT_EMPTY"] = "1"
    _apply_env(monkeypatch, env)
    cfg = config.load()
    assert cfg.expect_empty is True


def test_traffic_class_override(tmp_path, monkeypatch):
    env = _env(tmp_path)
    env["SHADOW_TRAFFIC_CLASS"] = "synthetic_bench"
    _apply_env(monkeypatch, env)
    cfg = config.load()
    assert cfg.traffic_class == "synthetic_bench"


@pytest.mark.parametrize("valid_value", ["real", "synthetic_bench", "fault_test"])
def test_traffic_class_valid_values_load(tmp_path, monkeypatch, valid_value):
    env = _env(tmp_path)
    env["SHADOW_TRAFFIC_CLASS"] = valid_value
    _apply_env(monkeypatch, env)
    cfg = config.load()
    assert cfg.traffic_class == valid_value


def test_traffic_class_invalid_value_rejected(tmp_path, monkeypatch):
    env = _env(tmp_path)
    env["SHADOW_TRAFFIC_CLASS"] = "bogus"
    _apply_env(monkeypatch, env)
    with pytest.raises(config.ConfigError) as exc_info:
        config.load()
    msg = str(exc_info.value)
    assert "bogus" in msg
    assert "real" in msg and "synthetic_bench" in msg and "fault_test" in msg


def test_fault_override(tmp_path, monkeypatch):
    env = _env(tmp_path)
    env["SHADOW_FAULT"] = "everos_timeout"
    _apply_env(monkeypatch, env)
    cfg = config.load()
    assert cfg.fault == "everos_timeout"


def test_invalid_port_value_rejected(tmp_path, monkeypatch):
    env = _env(tmp_path)
    env["EVEROS_MCP_PORT"] = "not-a-number"
    _apply_env(monkeypatch, env)
    with pytest.raises(config.ConfigError):
        config.load()
