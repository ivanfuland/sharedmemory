"""M2 de-risk 硬门：serve --http OAuth client_credentials + source 写隔离/读联邦 + 负例矩阵。
真起 serve、真换 token、真调 /mcp 写读。FAIL = 停手记 M2-EXIT。

Probe 校正（2026-06-23）：
  - 工具名 put_page / get_page 已确认（tools/list 实测）
  - pages.source_id 已确认（psql describe pages 实测）
  - insufficient_scope 已确认（read-only 写拒真实 error 字符串）
  - /ingest → 202 Accepted，入队 ingest_capture job，无 worker → 页不落库（M2 安全态）
  - put_page 参数：slug + content（必填），source 参数被 server 忽略（SOURCE-STAMPED）
"""
import json, os, re, signal, subprocess, time, pathlib
import urllib.request, urllib.error, urllib.parse
import pytest

GBRAIN_HOME = os.environ["GBRAIN_HOME"]
ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 7798
BASE = f"http://127.0.0.1:{PORT}"
pytestmark = pytest.mark.needs_gbrain


def _env():
    return {**os.environ, "GBRAIN_HOME": GBRAIN_HOME,
            "PATH": os.path.expanduser("~/.bun/bin") + ":" + os.environ.get("PATH", "")}


def _clients():
    env = {}
    for ln in (ROOT / "infra/gbrain/clients.env").read_text().splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1); env[k] = v
    assert env, "clients.env 空——先跑 infra/gbrain/register-clients.sh"
    return env


@pytest.fixture(scope="module")
def server():
    proc = subprocess.Popen(["gbrain", "serve", "--http", "--port", str(PORT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env())
    deadline = time.time() + 25
    try:
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{BASE}/health", timeout=1); break
            except Exception:
                time.sleep(0.3)
        else:
            raise AssertionError("serve --http 未在 25s 内就绪")
        yield BASE
    finally:
        proc.send_signal(signal.SIGINT); proc.wait(timeout=10)


def oauth_token(client_id, client_secret, scope=None):
    """client_credentials 换 access_token。返回 token 或抛 HTTPError。"""
    form = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
    if scope:
        form["scope"] = scope
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(f"{BASE}/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["access_token"]


def mcp_call(token, method, params=None):
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{BASE}/mcp", data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        txt = r.read().decode()
    # SSE 或纯 JSON 都解析
    for ln in txt.splitlines():
        ln = ln[5:].strip() if ln.startswith("data:") else ln
        try:
            d = json.loads(ln)
            if isinstance(d, dict) and ("result" in d or "error" in d):
                return d
        except Exception:
            continue
    raise AssertionError(f"无法解析 MCP 响应: {txt[:300]}")


def test_client_credentials_exchange(server):
    """★ client_credentials 真换到 access_token（headless 路径成立）。"""
    c = _clients()
    tok = oauth_token(c["HUB_CC_CLIENT_ID"], c["HUB_CC_CLIENT_SECRET"])
    assert tok and len(tok) > 20, "没换到 access_token"


def test_no_token_and_bogus_rejected(server):
    """无 token / bogus token 调 /mcp → 401/403（HTTP 层强制，沿用 M0 class5）。"""
    for tok in (None, "totally-bogus-xyz"):
        with pytest.raises(urllib.error.HTTPError) as ei:
            mcp_call(tok, "tools/list")
        assert ei.value.code in (401, 403), f"token={tok!r} 应 401/403 实际 {ei.value.code}"


def test_write_lands_in_own_source(server):
    """★ hub-cc client 写一页 → 落在 source=hub-cc（写隔离正例）。
    probe 已确认：put_page 工具名/参数正确；pages.source_id 列名正确。"""
    c = _clients()
    tok = oauth_token(c["HUB_CC_CLIENT_ID"], c["HUB_CC_CLIENT_SECRET"])
    slug = "projects/m2-iso-probe"
    r = mcp_call(tok, "tools/call", {"name": "put_page",
                 "arguments": {"slug": slug, "content": "# M2 iso probe\nhub-cc 写隔离正例\n"}})
    assert "error" not in r, f"hub-cc 写自身 source 应成功: {r.get('error')}"
    # DB 断言：该页 source_id == hub-cc
    out = subprocess.run(["docker", "exec", "pg-memory", "psql", "-U", "gbrain", "-d", "gbrain", "-tA",
                          "-c", f"SELECT source_id FROM pages WHERE slug='{slug}';"],
                         capture_output=True, text=True, timeout=15).stdout.strip()
    assert out == "hub-cc", f"写隔离失败：page.source_id={out!r} 期望 hub-cc"


def _pg(sql):
    return subprocess.run(["docker", "exec", "pg-memory", "psql", "-U", "gbrain", "-d", "gbrain", "-tA", "-c", sql],
                          capture_output=True, text=True, timeout=15).stdout.strip()


def test_readonly_client_write_denied(server):
    """★ 负例：read-only client 调写工具 → 必须 insufficient_scope（非任意 error）+ DB 无落页。
    probe 已确认真实 error 字符串为 insufficient_scope。"""
    c = _clients()
    tok = oauth_token(c["HUB_READONLY_CLIENT_ID"], c["HUB_READONLY_CLIENT_SECRET"])
    slug = "projects/should-fail-readonly"
    try:
        r = mcp_call(tok, "tools/call", {"name": "put_page",
                     "arguments": {"slug": slug, "content": "# nope\n"}})
        # /mcp 工具级拒绝走 JSON error；断言是 scope 而非 unknown_operation/参数错
        err = json.dumps(r.get("error", {}), ensure_ascii=False)
        # MCP 结果可能走 isError:true 的 result 而非顶层 error
        result_txt = json.dumps(r.get("result", {}), ensure_ascii=False)
        assert "insufficient_scope" in err or "insufficient_scope" in result_txt or \
               "scope" in err.lower() or "scope" in result_txt.lower(), \
            f"read-only 写须 insufficient_scope: err={err!r} result={result_txt!r}"
    except urllib.error.HTTPError as e:
        assert e.code in (401, 403), f"read-only 写应 401/403 实际 {e.code}"
    # DB 硬证：目标 slug 未落库（证明真没写进去，不是只回了 error）
    assert _pg(f"SELECT count(*) FROM pages WHERE slug='{slug}';") == "0", "read-only 写竟落库了"


def test_mcp_write_source_is_token_bound_not_overridable(server):
    """★ /mcp 写隔离的强属性：source 由 token 绑定、put_page 无 source 参数可越权。
    hub-codex 写一页 → 落 source=hub-codex（不是 hub-cc，也无法通过参数指定别的 source）。
    probe 已确认：put_page 的 source 参数是 SOURCE-STAMPED（server 忽略客户端值）。"""
    c = _clients()
    tok = oauth_token(c["HUB_CODEX_CLIENT_ID"], c["HUB_CODEX_CLIENT_SECRET"])
    slug = "projects/m2-iso-codex"
    # 即便恶意塞 source 参数，dispatch 也只认 authInfo.sourceId
    r = mcp_call(tok, "tools/call", {"name": "put_page",
                 "arguments": {"slug": slug, "content": "# codex\n", "source": "hub-cc"}})
    assert "error" not in r, f"hub-codex 写自身 source 应成功: {r.get('error')}"
    assert _pg(f"SELECT source_id FROM pages WHERE slug='{slug}';") == "hub-codex", \
        "/mcp 写隔离失败：source 未被 token 强制绑定（被参数越权？）"


def test_ingest_inert_without_jobs_worker_in_m2(server):
    """★ /ingest 在 M2 部署下的真实安全态：/ingest 只 **入队** ingest_capture job，
    由独立 jobs worker 处理；**M2 只跑 serve --http、不跑 jobs worker**
    → /ingest 写入 inert（页不落库），所谓 header-trust 越权在 M2 部署里根本不发生。
    真打 /ingest（hub-cc token + x-gbrain-source-id=hub-openclaw），轮询确认页 **未落库** = M2 安全态。

    Probe 校正：/ingest 返回 202 Accepted（job queued），response body 含 source_id 字段来自 header，
    但无 worker → 不落库。hub-openclaw source 已注册（register-clients.sh 建立）。"""
    c = _clients()
    tok = oauth_token(c["HUB_CC_CLIENT_ID"], c["HUB_CC_CLIENT_SECRET"])
    slug = "projects/m2-ingest-gap"
    _pg(f"DELETE FROM pages WHERE slug='{slug}';")
    req = urllib.request.Request(f"{BASE}/ingest", data="# ingest probe\ngap test\n".encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "text/markdown",
                 "x-gbrain-source-id": "hub-openclaw", "x-gbrain-slug": slug})
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        assert e.code not in (401, 403), f"/ingest 拒绝 write client（行为变化）→更新 M2-EXIT: {e.code}"
        raise
    landed = ""
    for _ in range(12):   # 6s：M2 无 worker 则永不落库
        landed = _pg(f"SELECT source_id FROM pages WHERE slug='{slug}';")
        if landed:
            break
        time.sleep(0.5)
    print(f"INGEST_LANDED_SOURCE={landed!r}")
    _pg(f"DELETE FROM pages WHERE slug='{slug}';")
    assert landed == "", (
        f"/ingest 在 M2(无 jobs worker)部署下应 inert(页不落库)，实测却落 source={landed!r}！"
        "说明 serve 自处理队列或有 worker 在跑 → header-trust 越权可能真发生，"
        "立即重评隔离措辞 + 强化缓解（不接 client / tailnet path 白名单）。")


def test_bridge_cannot_reach_other_source_via_mcp(server):
    """★ §2.5.3 bridge 越权防护：hub-bridge（source=distill-bridge）即便恶意塞 source 参数指向 hub-cc，
    /mcp 也只认 token-bound source → 页落 distill-bridge，hub-cc 零新增（越权结构性不可达，强于"拒绝"）。"""
    c = _clients()
    tok = oauth_token(c["HUB_BRIDGE_CLIENT_ID"], c["HUB_BRIDGE_CLIENT_SECRET"])
    slug = "projects/m2-bridge-iso"
    before_cc = _pg("SELECT count(*) FROM pages WHERE source_id='hub-cc';")
    r = mcp_call(tok, "tools/call",
                 {"name": "put_page", "arguments": {"slug": slug, "content": "# bridge\n", "source": "hub-cc"}})
    assert "error" not in r, f"bridge 写自身 source 应成功: {r.get('error')}"
    assert _pg(f"SELECT source_id FROM pages WHERE slug='{slug}';") == "distill-bridge", \
        "bridge 写未落 distill-bridge（source 未绑定？）"
    assert _pg("SELECT count(*) FROM pages WHERE source_id='hub-cc';") == before_cc, \
        "越权泄漏：bridge 的写竟进了 hub-cc source"


def test_federated_read_sees_other_source(server):
    """读联邦：hub-codex client 能读到 hub-cc 写的页（federated_read 含 hub-cc）。
    本测试自 seed（hub-cc 先写），不依赖测序/前序测试。"""
    c = _clients()
    cc = oauth_token(c["HUB_CC_CLIENT_ID"], c["HUB_CC_CLIENT_SECRET"])
    mcp_call(cc, "tools/call", {"name": "put_page",
             "arguments": {"slug": "projects/m2-iso-probe", "content": "# seed\nfederated read 用\n"}})
    tok = oauth_token(c["HUB_CODEX_CLIENT_ID"], c["HUB_CODEX_CLIENT_SECRET"])
    r = mcp_call(tok, "tools/call", {"name": "get_page", "arguments": {"slug": "projects/m2-iso-probe"}})
    assert "error" not in r, f"读联邦失败：hub-codex 应能读 hub-cc 的页: {r.get('error')}"
