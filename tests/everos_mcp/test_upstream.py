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


def test_search_oversized_2xx_body_maps_to_upstream_bad_response_not_internal(monkeypatch):
    """P1c:此前 `upstream.search` 只 catch `http.BadJson`,`http.ResponseTooLarge`
    (响应体超过 `http.MAX_RESPONSE_BYTES`)会滑过、被 server.py 最外层 broad
    except 误判成 error_code="internal"——这本质是上游响应异常(响应体不可信),
    应同 BadJson 一样落 everos_bad_response,不落 internal。monkeypatch 把上限
    调小,避免真的构造超大 fixture body(与 test_http.py 同一约定)。"""
    from everos_mcp import http

    monkeypatch.setattr(http, "MAX_RESPONSE_BYTES", 16)
    oversized = json.dumps({"request_id": "r1", "data": {"agent_cases": [], "agent_skills": []}}).encode("utf-8")
    assert len(oversized) > 16
    stub = _make_stub(200, oversized)
    try:
        cfg = _cfg(stub.base_url)
        with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
            upstream.search(cfg, "q")
        assert exc_info.value.code == "everos_bad_response"
    finally:
        stub.stop()


# ======================================================================
# normalize_candidates():完整 envelope + 违规校验
# ======================================================================

def _full_envelope():
    """候选 payload 含 prod passage 规格所需的必需字段(task_intent/approach、
    name/description)——P1f/P2a schema 校验落地后,缺这些字段的 fixture 会
    在 `normalize_candidates` 阶段就被拒(见下方专项测试),happy path fixture
    必须是"builder 真的能拼出 passage"的合法形状,不能只塞任意 `"text"` 占位
    字段。"""
    return {
        "request_id": "req-123",
        "data": {
            "agent_cases": [
                {"id": "ac_1", "score": 0.9, "task_intent": "调研 X", "approach": "先读 spec"},
                {"id": "ac_2", "score": 0.5, "task_intent": "修 Y", "approach": "二分定位"},
            ],
            "agent_skills": [
                {"id": "sk_1", "score": None, "name": "调研技能", "description": "先框架后细节"},
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
    assert c0.payload == {"id": "ac_1", "score": 0.9, "task_intent": "调研 X", "approach": "先读 spec"}

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


@pytest.mark.parametrize("non_object_resp", [[1, 2], "x", 3, None])
def test_normalize_top_level_non_object_maps_to_bad_response(non_object_resp):
    """`http.post_json` 只保证返回 `json.loads` 的结果——顶层合法 JSON 但非对象
    (数组/字符串/数字/null)之前会直接摔进 `resp.get(...)` 触发 AttributeError,
    被外层 broad except 误判成 internal。失败矩阵要求这类畸形信封一律
    everos_bad_response,顶层类型检查必须在 normalize_candidates 最前面。"""
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(non_object_resp)
    assert exc_info.value.code == "everos_bad_response"


# ======================================================================
# P1f/P2a:候选 payload 白名单字段 schema 校验——kill "KeyError/TypeError 滑到
# build_passage 才炸、落 internal" + "非 str 字段靠 clamp_payload 的
# isinstance 检查绕过截断上限(len(非 str)==0)"两个 bug
# ======================================================================

@pytest.mark.parametrize("bad_value", [["a", "list"], {"nested": "dict"}, 42, 3.14, True])
def test_normalize_whitelisted_field_wrong_type_maps_to_bad_response(bad_value):
    """`task_intent` 这类白名单字段如果是 list/dict/数值/bool——不是 str 也不是
    None——必须在 normalize 阶段就被拒,不能带着畸形类型继续走到
    `contract.clamp_payload`(那里非 str 值的"长度"被算成 0,截断上限被绕过)
    或 `probe_passage.build_passage`(那里"\\n".join 需要 str 元素,炸
    TypeError)。"""
    env = _full_envelope()
    env["data"]["agent_cases"][0]["task_intent"] = bad_value
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


def test_normalize_required_case_field_missing_maps_to_bad_response():
    """prod passage 规格(`_assemble_text`)需要 task_intent+approach 都在场
    才能拼出 passage——缺任一个,`payload[field]` 会原生 KeyError。schema
    校验必须提前把这类"缺失必需字段"的候选挡在 normalize 阶段。"""
    env = _full_envelope()
    del env["data"]["agent_cases"][0]["approach"]
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


def test_normalize_required_case_field_none_maps_to_bad_response():
    """必需字段显式为 None——`"\\n".join(...)` 需要 str,None 会原生 TypeError
    (与"缺失"效果一致,同样必须提前拒)。"""
    env = _full_envelope()
    env["data"]["agent_cases"][0]["task_intent"] = None
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


def test_normalize_required_skill_field_missing_maps_to_bad_response():
    env = _full_envelope()
    del env["data"]["agent_skills"][0]["name"]
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"


def test_normalize_optional_case_field_missing_is_accepted():
    """`key_insight` 只在 "full" 规格才被 build_passage 用到,shadow 固定用
    "prod" 规格——缺失/None 都合法(clamp_payload 自己会把它当 0 长度处理),
    不该被 schema 校验误伤。"""
    env = _full_envelope()
    assert "key_insight" not in env["data"]["agent_cases"][0]  # fixture 本就没给
    result = upstream.normalize_candidates(env)
    assert len(result.cases) == 2


def test_normalize_optional_case_field_none_is_accepted():
    env = _full_envelope()
    env["data"]["agent_cases"][0]["key_insight"] = None
    result = upstream.normalize_candidates(env)
    assert len(result.cases) == 2


def test_normalize_optional_skill_field_wrong_type_still_rejected():
    """可选字段(content)缺失/None 合法,但类型非法(既非 str 也非 None)仍要拒
    ——"可选"只豁免"没给",不豁免"给了但类型是垃圾"。"""
    env = _full_envelope()
    env["data"]["agent_skills"][0]["content"] = ["not", "a", "string"]
    with pytest.raises(upstream.UpstreamBadResponse) as exc_info:
        upstream.normalize_candidates(env)
    assert exc_info.value.code == "everos_bad_response"
