"""Task 7: in-session MCP pull via scoped bearer + token lifecycle hard verification.

Hits the LIVE running service on 127.0.0.1:7777 (gbrain-mcp.service, Task2).
No fixture server spawning — the service must already be active.

Probe basis (Task1 + Task2 reports):
  - /token → client_credentials, returns access_token + expires_in
  - /mcp   → JSON-RPC 2.0, Bearer auth, tools/list returns HTTP 200
  - hub-cc token_ttl=2592000 (30d) — set by register-clients.sh SQL
  - hub-shortlived token_ttl=2 — set by Task1 SQL
"""
import json
import os
import pathlib
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:7777"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clients() -> dict:
    """Parse infra/gbrain/clients.env into a dict of env-var-style keys."""
    env: dict = {}
    path = ROOT / "infra/gbrain/clients.env"
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            env[k] = v
    assert env, f"clients.env 空——先跑 infra/gbrain/register-clients.sh (path={path})"
    return env


def _token_resp(client_id: str, client_secret: str) -> dict:
    """Mint a client_credentials access_token. Returns full JSON response dict."""
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _token(client_id: str, client_secret: str) -> str:
    return _token_resp(client_id, client_secret)["access_token"]


def _mcp_list(tok: str) -> int:
    """POST tools/list to /mcp. Returns HTTP status code (or raises HTTPError)."""
    payload = {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1}
    req = urllib.request.Request(
        f"{BASE}/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {tok}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_service_reachable():
    """Pre-check: service on :7777 must be alive before any MCP tests run."""
    try:
        body = urllib.request.urlopen(f"{BASE}/health", timeout=5).read()
        assert body, "/health 无响应"
    except Exception as e:
        pytest.fail(
            f"gbrain service 未运行在 {BASE}——先确认 gbrain-mcp.service active: {e}"
        )


def test_prod_clients_have_30d_ttl():
    """★ 防 silent no-op 假绿：prod client /token 真返 expires_in==2592000（30d）。

    证明 register-clients.sh 的 token_ttl UPDATE 真生效——issuer 真采用此 TTL，
    不止是 DB 列改了但 OAuth provider 没读到。
    """
    c = _clients()
    resp = _token_resp(c["HUB_CC_CLIENT_ID"], c["HUB_CC_CLIENT_SECRET"])
    assert resp.get("expires_in") == 2592000, (
        f"hub-cc token expires_in 应=2592000(30d)，"
        f"实测 {resp.get('expires_in')} — register-clients.sh SQL 未生效？"
    )


def test_in_session_pull_via_mcp():
    """In-session MCP tools/list with a hub-cc scoped token → HTTP 200."""
    c = _clients()
    tok = _token(c["HUB_CC_CLIENT_ID"], c["HUB_CC_CLIENT_SECRET"])
    status = _mcp_list(tok)
    assert status == 200, f"in-session MCP tools/list 应 200，实测 {status}"


def test_token_expiry_then_remint_recovers():
    """★ token 生命周期硬验：hub-shortlived TTL=2s。

    Flow: mint → 200 → wait 4s (超过 2s TTL) → 401/403 → re-mint → 200.

    Verifies:
    - client_credentials 无 refresh token（expired = hard 401, not auto-renewed）
    - re-mint（新 POST /token）能恢复 200（恢复路径真通）
    - TTL 设置非虚设（若 TTL 不生效则 token 不过期，expired==False → test FAIL + report）
    """
    c = _clients()
    cid = c["HUB_SHORTLIVED_CLIENT_ID"]
    cs = c["HUB_SHORTLIVED_CLIENT_SECRET"]

    # Fresh token should work immediately
    tok = _token(cid, cs)
    assert _mcp_list(tok) == 200, "新 token 应 200（hub-shortlived mint 失败？）"

    # Wait beyond the 2s TTL
    time.sleep(4)

    # Expired token must be rejected
    expired = False
    try:
        _mcp_list(tok)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            expired = True
        else:
            pytest.fail(f"期望 401/403，实测 HTTP {e.code}")

    assert expired, (
        "过期 token 未被拒（仍 200）——hub-shortlived TTL 形同虚设。"
        " Task1 SQL UPDATE token_ttl=2 是否真生效？"
        " 见 task-7-report.md 「token-expiry 实测」段。"
    )

    # Re-mint → should recover
    new_tok = _token(cid, cs)
    assert _mcp_list(new_tok) == 200, "re-mint 后应恢复 200（恢复路径真通）"


def test_refresh_wrapper_produces_working_token():
    """★ 证 token-refresh.sh artifact 产的 token 文件能打 /mcp 200.

    Runs the actual wrapper script (HUB_CC), reads ~/.config/gbrain/hub-cc.token,
    then calls /mcp tools/list with that token.

    Side-effect: writes ~/.config/gbrain/hub-cc.token (harmless token cache).
    Does NOT run connect-cc.sh / gbrain connect --install (deferred activation).
    """
    script = ROOT / "infra/gbrain/mcp/token-refresh.sh"
    assert script.exists(), f"token-refresh.sh 不存在: {script}"

    env = {**os.environ, "PATH": os.path.expanduser("~/.bun/bin") + ":" + os.environ.get("PATH", "")}
    r = subprocess.run(
        ["bash", str(script), "HUB_CC"],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    assert r.returncode == 0, (
        f"token-refresh.sh 失败 (rc={r.returncode}):\n"
        f"stdout: {r.stdout[:300]}\nstderr: {r.stderr[:300]}"
    )

    token_file = pathlib.Path(os.path.expanduser("~/.config/gbrain/hub-cc.token"))
    assert token_file.exists(), f"token 文件未生成: {token_file}"

    tok = token_file.read_text().strip()
    assert tok, "token 文件为空"
    assert _mcp_list(tok) == 200, (
        f"wrapper 产的 token 无法打 /mcp 200 (token={tok[:20]}...)"
    )
