"""feeder 纯逻辑件测试。fixture 全合成(PUBLIC 仓铁律)。"""
import sqlite3 as _sqlite3

import httpx
import pytest

from scripts.everos_feed_session import _AddCountingHttpx, _is_pre_add_transient, _read_rows, _wait_terminal


class _FakeResp:
    def __init__(self, code):
        self.status_code = code


class _FakeHttpx:
    """替身 real httpx:按预置队列返回响应或抛异常。"""
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(url)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item)


def test_counting_hook_counts_only_successful_add():
    fake = _FakeHttpx([200, 422, 200])
    probe = _AddCountingHttpx(fake)
    assert probe.post("http://x/api/v1/memory/add").status_code == 200
    assert probe.add_ok == 1
    probe.post("http://x/api/v1/memory/add")          # 422:不计数
    assert probe.add_ok == 1
    probe.post("http://x/api/v1/memory/flush")        # flush:不计数
    assert probe.add_ok == 1


def test_counting_hook_passes_exceptions_through():
    fake = _FakeHttpx([httpx.ConnectError("boom")])
    probe = _AddCountingHttpx(fake)
    with pytest.raises(httpx.ConnectError):
        probe.post("http://x/api/v1/memory/add")
    assert probe.add_ok == 0


def _status_error(code):
    req = httpx.Request("POST", "http://x/api/v1/memory/add")
    return httpx.HTTPStatusError("err", request=req, response=httpx.Response(code, request=req))


@pytest.mark.parametrize("exc,add_ok,expected", [
    (httpx.ConnectError("x"), 0, True),      # 连接类:请求没到实例,零副作用 → 可退避
    (httpx.ConnectTimeout("x"), 0, True),
    (_status_error(422), 0, True),           # 422 busy:实例收到并拒绝,零副作用 → 可退避(M1b 实证)
    (_status_error(500), 0, False),          # 5xx:语义不明,可能已部分处理 → 不重试
    (httpx.ReadTimeout("x"), 0, False),      # 响应缺失:请求可能已落地 → 不重试
    (httpx.ConnectError("x"), 1, False),     # 已有 /add 落地:重放整个 run_session 会重复喂前缀 → 一律不重试
    (_status_error(422), 2, False),
])
def test_is_pre_add_transient(exc, add_ok, expected):
    assert _is_pre_add_transient(exc, add_ok) is expected


@pytest.mark.parametrize("exc,add_ok,expected", [
    (httpx.ConnectError("x"), 0, True),      # 请求没到达 → 确定零副作用
    (httpx.ConnectTimeout("x"), 0, True),
    (_status_error(422), 0, True),           # 4xx = 服务端收到并拒绝 → 确定零副作用
    (_status_error(429), 0, True),           # 预算/限流拒(spec §5:首个 /add 前预算拒 → 回 pending)
    (_status_error(402), 0, True),
    (_status_error(500), 0, False),          # 5xx:可能已部分处理 → 不能按零副作用回 pending
    (httpx.ReadTimeout("x"), 0, False),      # 请求可能已落地 → 不能
    (_status_error(422), 1, False),          # 已有 /add 落地 → 一律不是零副作用
])
def test_is_no_side_effect(exc, add_ok, expected):
    from scripts.everos_feed_session import _is_no_side_effect
    assert _is_no_side_effect(exc, add_ok) is expected


def _mk_cass(tmp_path, external_id="s1", created=(1000, 2000, 3000)):
    """最小合成 CASS 库:conversations + messages 两表(只含 feeder 用到的列)。"""
    db = tmp_path / "cass.db"
    con = _sqlite3.connect(db)
    con.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, external_id TEXT)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INT, idx INT,"
                " role TEXT, content TEXT, created_at INT, extra_bin BLOB, extra_json TEXT)")
    con.execute("INSERT INTO conversations VALUES (1, ?)", (external_id,))
    for i, ts in enumerate(created):
        con.execute("INSERT INTO messages VALUES (NULL, 1, ?, 'user', 'hello', ?, NULL, '{}')", (i, ts))
    con.commit()
    con.close()
    return str(db)


def test_read_rows_returns_rows_and_payload_max(tmp_path):
    db = _mk_cass(tmp_path)
    rows, payload_max, found = _read_rows(db, "s1")
    assert len(rows) == 3
    assert payload_max == 3000
    assert found is True
    assert rows[0]["role"] == "user"


def test_read_rows_conv_not_found(tmp_path):
    db = _mk_cass(tmp_path)
    rows, payload_max, found = _read_rows(db, "missing")
    assert rows == [] and payload_max is None and found is False


def test_read_rows_ignores_null_created_at(tmp_path):
    """终审 Minor-1:adapter 层容忍 NULL created_at,feeder 的 max() 不该因此 TypeError。
    混合 (1000, None, 3000) → payload_max 取非 None 中的最大值;全 None → payload_max is None。"""
    db = _mk_cass(tmp_path, created=(1000, None, 3000))
    rows, payload_max, found = _read_rows(db, "s1")
    assert len(rows) == 3
    assert payload_max == 3000
    assert found is True

    sub = tmp_path / "allnull"
    sub.mkdir()
    db_all_null = _mk_cass(sub, created=(None, None))
    rows2, payload_max2, found2 = _read_rows(db_all_null, "s1")
    assert len(rows2) == 2
    assert payload_max2 is None
    assert found2 is True


def test_wait_terminal_returns_ids_when_case_appears(tmp_path):
    md = tmp_path / "agents" / "a" / ".cases"
    md.mkdir(parents=True)
    (md / "agent_case-2026-07-14.md").write_text(
        "<!-- entry:ac_1 -->\n## ac_1\n\n**session_id**: prod-abc\n<!-- /entry:ac_1 -->\n",
        encoding="utf-8")
    ids = _wait_terminal(str(tmp_path), "prod-abc", window_s=2, poll_s=1)
    assert ids == ["ac_1"]


def test_wait_terminal_times_out_empty(tmp_path):
    ids = _wait_terminal(str(tmp_path), "prod-none", window_s=1, poll_s=1)
    assert ids == []


# ── 真 exec 集成 smoke:subprocess 跑 `python -m scripts.everos_feed_session`,
# stub HTTP server 扮演 EverOS,合成 sqlite(_mk_cass)扮演 CASS(Task 3)。

import json as _json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class _FakeEverOS(BaseHTTPRequestHandler):
    hits = []  # class-level:记录 (path)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        type(self).hits.append(self.path)
        body = _json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静音
        pass


def _run_feeder(env_extra, external_id="s1", claim_ts=999999, sid="prod-itest"):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **env_extra}
    p = subprocess.run(
        [sys.executable, "-m", "scripts.everos_feed_session",
         f"--external-id={external_id}", f"--claim-msg-ts={claim_ts}", f"--short-sid={sid}"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    return p


def _seed_case(md_root: Path, sid: str):
    d = md_root / "agents" / "everos-prod" / ".cases"
    d.mkdir(parents=True)
    (d / "agent_case-2026-07-14.md").write_text(
        f"<!-- entry:ac_9 -->\n## ac_9\n\n**session_id**: {sid}\n<!-- /entry:ac_9 -->\n",
        encoding="utf-8")


def test_e2e_completed(tmp_path):
    """真 exec:合成库 → stub /add+/flush → 预埋卡 → stdout completed + entry id。"""
    db = _mk_cass(tmp_path)
    _FakeEverOS.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeEverOS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    md_root = tmp_path / "md"
    _seed_case(md_root, "prod-itest")
    try:
        p = _run_feeder({
            "EVEROS_CASS_DB": db,
            "EVEROS_PROD_BASE_URL": f"http://127.0.0.1:{srv.server_port}",
            "EVEROS_PROD_MD_ROOT": str(md_root),
            "EVEROS_TERMINAL_WINDOW_S": "5",
        })
    finally:
        srv.shutdown()
    assert p.returncode == 0, p.stderr
    out = _json.loads(p.stdout.strip().splitlines()[-1])
    assert out["status"] == "completed"
    assert out["case_entry_ids"] == ["ac_9"]
    assert out["payload_max_created_at"] == 3000
    assert any(h.endswith("/memory/add") for h in _FakeEverOS.hits)
    assert any(h.endswith("/memory/flush") for h in _FakeEverOS.hits)


def test_e2e_stale_zero_http(tmp_path):
    """claim_msg_ts 落后于 payload → stale,且一个 HTTP 都不发(冷却核对在 /add 之前)。"""
    db = _mk_cass(tmp_path)  # payload_max=3000
    _FakeEverOS.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeEverOS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = _run_feeder({
            "EVEROS_CASS_DB": db,
            "EVEROS_PROD_BASE_URL": f"http://127.0.0.1:{srv.server_port}",
            "EVEROS_PROD_MD_ROOT": str(tmp_path / "md"),
        }, claim_ts=2500)
    finally:
        srv.shutdown()
    out = _json.loads(p.stdout.strip().splitlines()[-1])
    assert out["status"] == "stale"
    assert _FakeEverOS.hits == []


def test_e2e_instance_down_no_side_effect(tmp_path):
    """实例不可达:退避耗尽 → no_side_effect_error(回 pending 语义)。
    退避 sleep 写死 M1b 口径(15/30/45s),本用例真等 ~90s——慢但每次 PR 前全量必跑,
    它是 once-only 零副作用回退唯一的真 exec 证据。
    URL 用 localhost 不用回环 IP 字面量:避免 PUBLIC 仓敏感扫描 regex 自命中(R1-P2-3)。"""
    db = _mk_cass(tmp_path)
    p = _run_feeder({
        "EVEROS_CASS_DB": db,
        "EVEROS_PROD_BASE_URL": "http://localhost:9",  # 端口 9(discard):必 ConnectError
        "EVEROS_PROD_MD_ROOT": str(tmp_path / "md"),
    })
    out = _json.loads(p.stdout.strip().splitlines()[-1])
    assert out["status"] == "no_side_effect_error"


class _Fake422ThenOkEverOS(_FakeEverOS):
    """首个 /add 返 422(M1b busy 实证形态),其后全 200——退避应吸收(codex R7 + spec §6.1)。"""
    rejected = False

    def do_POST(self):
        if self.path.endswith("/memory/add") and not type(self).rejected:
            type(self).rejected = True
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            type(self).hits.append(self.path + "#422")
            self.send_response(422)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_POST()


def test_e2e_first_add_422_backoff_absorbed(tmp_path):
    """真实 raise_for_status 路径(R1 决策1保留意见):首个 /add 422 → 15s 退避 → 重试成功 → completed。
    这是验收 §6.1「人为制造首 add 前瞬时失败,退避吸收后正常 completed 而非落 running」的测试化。"""
    db = _mk_cass(tmp_path)
    _Fake422ThenOkEverOS.hits = []
    _Fake422ThenOkEverOS.rejected = False
    srv = HTTPServer(("127.0.0.1", 0), _Fake422ThenOkEverOS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    md_root = tmp_path / "md"
    _seed_case(md_root, "prod-itest")
    try:
        p = _run_feeder({
            "EVEROS_CASS_DB": db,
            "EVEROS_PROD_BASE_URL": f"http://127.0.0.1:{srv.server_port}",
            "EVEROS_PROD_MD_ROOT": str(md_root),
            "EVEROS_TERMINAL_WINDOW_S": "5",
        })
    finally:
        srv.shutdown()
    out = _json.loads(p.stdout.strip().splitlines()[-1])
    assert out["status"] == "completed", (out, p.stderr)
    assert any(h.endswith("#422") for h in _Fake422ThenOkEverOS.hits)          # 真吃过一次 422
    assert any(h.endswith("/memory/flush") for h in _Fake422ThenOkEverOS.hits)  # 退避后走完全程


# ── T2 review follow-up:main 状态机盲区补测(控制面增补,非 brief verbatim) ──


def test_e2e_skipped_no_terminal_within_window(tmp_path):
    """正常 completed 场景的装配(/add+/flush 全 200)但不预埋 case 文件、终态窗口极短
    (EVEROS_TERMINAL_WINDOW_S=2) → 真喂到底(/add + /flush 都打了)但终态窗口空手而归,
    落 skipped/no_terminal_within_window(非 completed,也非 conv_not_found 那种早退 skipped)。"""
    db = _mk_cass(tmp_path)
    _FakeEverOS.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeEverOS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = _run_feeder({
            "EVEROS_CASS_DB": db,
            "EVEROS_PROD_BASE_URL": f"http://127.0.0.1:{srv.server_port}",
            "EVEROS_PROD_MD_ROOT": str(tmp_path / "md"),  # 不预埋 case
            "EVEROS_TERMINAL_WINDOW_S": "2",
        })
    finally:
        srv.shutdown()
    assert p.returncode == 0, p.stderr
    out = _json.loads(p.stdout.strip().splitlines()[-1])
    assert out["status"] == "skipped"
    assert out["detail"] == "no_terminal_within_window"
    assert out["case_entry_ids"] == []
    assert any(h.endswith("/memory/add") for h in _FakeEverOS.hits)     # 真喂了
    assert any(h.endswith("/memory/flush") for h in _FakeEverOS.hits)   # 喂完了,只是超窗未出卡


class _Fake500ThenOkEverOS(_FakeEverOS):
    """首个 /add 返 500(语义不明,既不属 pre-add 可退避集也不属零副作用集)——
    不应重试,一次定性为 error;其后即使转 200 也不该被打到(不该有第二次 /add 请求)。"""
    rejected = False

    def do_POST(self):
        if self.path.endswith("/memory/add") and not type(self).rejected:
            type(self).rejected = True
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            type(self).hits.append(self.path + "#500")
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_POST()


def test_e2e_first_add_500_error_keeps_ambiguity(tmp_path):
    """首个 /add 返 500 → `_is_pre_add_transient`/`_is_no_side_effect` 均判 False(5xx 语义
    不明,可能已部分处理)→ 不重试,一次定性为 error(worker 保持 running 归 nightly)。
    这是 `_is_pre_add_transient`/`_is_no_side_effect` 表驱动单测(422/ConnectError 走可退避
    或零副作用分支)在 main 状态机里唯一未覆盖的「不退避不 no_side_effect,直接 error」分岔。"""
    db = _mk_cass(tmp_path)
    _Fake500ThenOkEverOS.hits = []
    _Fake500ThenOkEverOS.rejected = False
    srv = HTTPServer(("127.0.0.1", 0), _Fake500ThenOkEverOS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p = _run_feeder({
            "EVEROS_CASS_DB": db,
            "EVEROS_PROD_BASE_URL": f"http://127.0.0.1:{srv.server_port}",
            "EVEROS_PROD_MD_ROOT": str(tmp_path / "md"),
            "EVEROS_TERMINAL_WINDOW_S": "5",
        })
    finally:
        srv.shutdown()
    assert p.returncode == 0, p.stderr
    out = _json.loads(p.stdout.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "add_ok=0" in out["detail"]
    add_hits = [h for h in _Fake500ThenOkEverOS.hits if "/memory/add" in h]
    assert len(add_hits) == 1  # 一次定性为 error,不许有第二次 /add 请求
