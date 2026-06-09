"""第5类：serve --http 鉴权（M0 范围 = HTTP 层鉴权强制）。
实测真实模型：OAuth 2.1（/authorize+/token+PKCE），admin bootstrap token 打到 stdout，
无 `gbrain auth` CLI——scoped client 经 /admin UI 或 DCR 注册。
M0 验证核心安全属性：MCP 端点强制鉴权（无 token / bogus token → 401）。
细粒度 scope/source 负例（read-only 写拒 / bridge 越权 source 拒）依赖 OAuth client provisioning，
归 M2（三端 scoped client 实际接入时）——见 contracts/gbrain-api-findings.md。"""
import os
import re
import signal
import subprocess
import time
import urllib.request
import urllib.error
import pathlib
import pytest

GBRAIN_HOME = os.environ["GBRAIN_HOME"]
PORT = 7799
BASE = f"http://127.0.0.1:{PORT}"
pytestmark = pytest.mark.needs_gbrain


def _env():
    return {**os.environ, "GBRAIN_HOME": GBRAIN_HOME,
            "PATH": os.path.expanduser("~/.bun/bin") + ":" + os.environ.get("PATH", "")}


@pytest.fixture
def server():
    import threading
    proc = subprocess.Popen(["gbrain", "serve", "--http", "--port", str(PORT)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=_env())
    box = {"admin_token": None, "lines": []}

    def drain():
        for line in proc.stdout:
            box["lines"].append(line)
            m = re.search(r"\b([0-9a-f]{40,})\b", line)
            if m:
                box["admin_token"] = (box["admin_token"] or "") + m.group(1)
    threading.Thread(target=drain, daemon=True).start()

    ready = False
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.3)
    try:
        assert ready, "serve --http 未在 20s 内就绪（/health 不通）"
        for _ in range(10):
            if box["admin_token"]:
                break
            time.sleep(0.3)
        assert box["admin_token"], "未抓到 admin bootstrap token——admin auth 未生效"
        yield box
    finally:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)


def _post_mcp(token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{BASE}/mcp", data=b'{"jsonrpc":"2.0","method":"tools/list","id":1}',
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=5)


def test_oauth_metadata_published(server):
    """OAuth 2.1 元数据端点存在（authorization_endpoint/token_endpoint/PKCE）。"""
    import json
    resp = urllib.request.urlopen(f"{BASE}/.well-known/oauth-authorization-server", timeout=5)
    meta = json.loads(resp.read())
    assert meta.get("token_endpoint") and meta.get("authorization_endpoint"), "OAuth 端点缺失"
    assert "S256" in (meta.get("code_challenge_methods_supported") or []), "PKCE S256 缺失"


def test_mcp_requires_auth_no_token(server):
    """核心安全属性：MCP 端点无 token 写 → 401（鉴权在 HTTP 层强制）。"""
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post_mcp(token=None)
    assert ei.value.code in (401, 403), f"无 token 调 /mcp 应 401/403，实际 {ei.value.code}"


def test_mcp_rejects_bogus_token(server):
    """bogus bearer token → 401/403（模型无关，证明 token 真被校验，非摆设）。"""
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post_mcp(token="totally-bogus-token-xyz")
    assert ei.value.code in (401, 403), f"bogus token 应 401/403，实际 {ei.value.code}"
