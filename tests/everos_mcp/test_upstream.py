"""upstream.search / upstream.normalize_candidates 契约测试(见任务简报)。

固定纪律:
- `search()` 的请求体逐字段对齐 `everos_eval/retrieve.py::search`
  (agent_id/query/method="hybrid"/top_k=20/enable_llm_rerank=False)。
- `normalize_candidates()` 喂**完整 envelope** fixture(`request_id` + `data`
  下的 `agent_cases`/`agent_skills`),覆盖 request_id 缺失/空串/非字符串、
  数组缺失、id 重复(含跨数组重复)、native score 非 finite 五类违规,
  全部落 `UpstreamBadResponse(code="everos_bad_response")`。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from everos_mcp import config, upstream


def _cfg(everos_base: str) -> config.Config:
    return config.Config(
        port=1,
        token="test-token",
        everos_base=everos_base,
        agent_id="test-agent",
        infinity_base="http://127.0.0.1:1",
        ledger_dir=None,
        expect_empty=False,
        embed_model="test-embed-model",
        rerank_model="test-rerank-model",
        pin_file=None,
        instance_dir=None,
        infinity_container="test-container",
        traffic_class="real",
        fault=None,
    )


class _Counter:
    def __init__(self):
        self.lock = threading.Lock()
        self.n = 0
        self.last_body: dict | None = None

    def bump(self, body: dict) -> int:
        with self.lock:
            self.n += 1
            self.last_body = body
            return self.n


class _Stub:
    def __init__(self, status: int, payload_or_bytes):
        self.counter = _Counter()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = {}
                outer.counter.bump(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if isinstance(payload_or_bytes, (bytes, bytearray)):
                    out = bytes(payload_or_bytes)
                else:
                    out = json.dumps(payload_or_bytes).encode("utf-8")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _make_stub(status, payload) -> _Stub:
    return _Stub(status, payload).start()


# ======================================================================
# search():请求体形状 + 端点路径
# ======================================================================

def test_search_request_body_matches_retrieve_shape():
    stub = _make_stub(200, {"request_id": "r1", "data": {"agent_cases": [], "agent_skills": []}})
    try:
        cfg = _cfg(stub.base_url)
        result = upstream.search(cfg, "my query")
        assert result == {"request_id": "r1", "data": {"agent_cases": [], "agent_skills": []}}
        assert stub.counter.n == 1
        assert stub.counter.last_body == {
            "agent_id": "test-agent",
            "query": "my query",
            "method": "hybrid",
            "top_k": 20,
            "enable_llm_rerank": False,
        }
    finally:
        stub.stop()


def test_search_http_error_maps_to_upstream_http_error_with_truncated_body():
    body = b"x" * 3000
    stub = _make_stub(500, body)
    try:
        cfg = _cfg(stub.base_url)
        with pytest.raises(upstream.UpstreamHTTPError) as exc_info:
            upstream.search(cfg, "q")
        assert exc_info.value.code == "everos_http_error"
        # body 截断到前 2000 字节,进异常消息(诊断用),不代表用户可见字段
        assert "x" * 2000 in str(exc_info.value)
        assert "x" * 2001 not in str(exc_info.value)
    finally:
        stub.stop()


def test_search_bad_json_maps_to_upstream_bad_response():
    stub = _make_stub(200, b"\xff\xfe\x00\x01")
    try:
        cfg = _cfg(stub.base_url)
        with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
            upstream.search(cfg, "q")
        assert exc_info.value.code == "everos_bad_response"
    finally:
        stub.stop()


def test_search_non_loopback_refused():
    cfg = _cfg("http://203.0.113.5:80")
    with pytest.raises(ValueError):
        upstream.search(cfg, "q")


# ======================================================================
# normalize_candidates():完整 envelope + 违规校验
# ======================================================================

def _full_envelope():
    return {
        "request_id": "req-123",
        "data": {
            "agent_cases": [
                {"id": "ac_1", "score": 0.9, "text": "case one"},
                {"id": "ac_2", "score": 0.5, "text": "case two"},
            ],
            "agent_skills": [
                {"id": "sk_1", "score": None, "text": "skill one"},
            ],
        },
    }


def test_normalize_full_envelope_happy_path():
    result = upstream.normalize_candidates(_full_envelope())
    assert result.everos_request_id == "req-123"
    assert len(result.cases) == 2
    assert len(result.skills) == 1

    c0 = result.cases[0]
    assert c0.id == "ac_1"
    assert c0.mem_type == "agent_case"
    assert c0.native_score == 0.9
    assert c0.source_rank == 0
    assert c0.payload == {"id": "ac_1", "score": 0.9, "text": "case one"}

    c1 = result.cases[1]
    assert c1.source_rank == 1

    s0 = result.skills[0]
    assert s0.native_score is None
    assert s0.mem_type == "agent_skill"


@pytest.mark.parametrize("bad_request_id", [None, "", 42, 3.14, [], {}])
def test_normalize_request_id_violations(bad_request_id):
    env = _full_envelope()
    env["request_id"] = bad_request_id
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


def test_normalize_missing_data_object():
    env = _full_envelope()
    del env["data"]
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


def test_normalize_missing_agent_cases_array():
    env = _full_envelope()
    del env["data"]["agent_cases"]
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


def test_normalize_missing_agent_skills_array():
    env = _full_envelope()
    del env["data"]["agent_skills"]
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


def test_normalize_duplicate_id_within_same_array():
    env = _full_envelope()
    env["data"]["agent_cases"].append({"id": "ac_1", "score": 0.1})
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


def test_normalize_duplicate_id_across_arrays():
    env = _full_envelope()
    env["data"]["agent_skills"].append({"id": "ac_1", "score": 0.2})  # 撞 agent_cases 里的 ac_1
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


@pytest.mark.parametrize("bad_id", ["", None, 42])
def test_normalize_empty_or_non_string_id(bad_id):
    env = _full_envelope()
    env["data"]["agent_cases"][0]["id"] = bad_id
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


@pytest.mark.parametrize("bad_score", [float("nan"), float("inf"), float("-inf"), "0.9", True])
def test_normalize_non_finite_native_score(bad_score):
    env = _full_envelope()
    env["data"]["agent_cases"][0]["score"] = bad_score
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


def test_normalize_native_score_none_is_accepted():
    env = _full_envelope()
    env["data"]["agent_cases"][0]["score"] = None
    result = upstream.normalize_candidates(env)
    assert result.cases[0].native_score is None
