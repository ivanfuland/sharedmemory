"""
Task 2: gbrain-mcp.service 存活 + 绑定 + 嵌入路径测试

前提: install-service.sh 已跑完（服务 active）。
工具名/参数来源: Task1 probe（task-1-report.md）+ tools/list 二次确认：
  tool=query, 参数名=query（非 question）。
"""
import subprocess, urllib.request
import pytest

def _systemctl(*a):
    return subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True, timeout=15)


def test_service_active():
    r = _systemctl("is-active", "gbrain-mcp.service")
    assert r.stdout.strip() == "active", f"gbrain-mcp 未 active: {r.stdout.strip()} / 先跑 install-service.sh"


def test_health_on_localhost():
    body = urllib.request.urlopen("http://127.0.0.1:7777/health", timeout=5).read()
    assert body, "/health 无响应"


def test_not_bound_to_wildcard():
    # 必须绑 127.0.0.1，不得 0.0.0.0（MEMORY.md tailscale serve 硬坑）
    out = subprocess.run(
        ["bash", "-lc", "ss -tlnp | grep ':7777' || true"],
        capture_output=True, text=True, timeout=10
    ).stdout
    assert "127.0.0.1:7777" in out, f"7777 应绑 127.0.0.1: {out!r}"
    assert "0.0.0.0:7777" not in out and ":::7777" not in out, f"禁绑 0.0.0.0/::: {out!r}"


def test_embedding_path_via_service_mcp_not_local_cli():
    """★ 经 systemd serve 进程的 /mcp query 验嵌入（codex R2#new4）：本地 CLI 用 shell env、
    证不了 unit 的 EnvironmentFile 是否被行内注释污染。必须打服务进程。
    query 工具名/参数以 probe 为准：tool=query, arg=query（task-1-report §1.2 + tools/list）。"""
    import json, os, pathlib, urllib.parse

    root = pathlib.Path(os.path.expanduser("~/projects/sharedmemory"))
    env = {}
    for ln in (root / "infra/gbrain/clients.env").read_text().splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            env[k] = v

    # Mint token via client_credentials
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": env["HUB_CC_CLIENT_ID"],
        "client_secret": env["HUB_CC_CLIENT_SECRET"],
    }).encode()
    treq = urllib.request.Request(
        "http://127.0.0.1:7777/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    tok = json.load(urllib.request.urlopen(treq, timeout=10))["access_token"]

    # Call query tool (arg name confirmed: query, not question)
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "query", "arguments": {"query": "光合作用"}},
        "id": 1,
    }
    mreq = urllib.request.Request(
        "http://127.0.0.1:7777/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {tok}",
        },
    )
    txt = urllib.request.urlopen(mreq, timeout=25).read().decode()
    compact = txt.replace(" ", "")
    # MCP scope errors walk result.isError=true (task-1-report §1.4); top-level "error" = protocol error
    assert '"isError":true' not in compact and '"error":{"' not in compact, \
        f"服务进程 /mcp query 报错（unit env 坏？）: {txt[:300]}"
