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
