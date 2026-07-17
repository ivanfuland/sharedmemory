"""server.py 的测试(P4 Task 8:fastmcp 组装 + 启动自检 + watchdog)。

固定纪律(见 everos_mcp/server.py 顶部文档字符串 + 任务简报,均为冻结项):
- 处理链顺序:① ops started(先于契约门,失败→os._exit(86)) → ② 契约门 →
  ③ checkpoint overdue 短路(先于 upstream) → ④ upstream 失败矩阵 →
  ⑤ 空结果/compute_returned → ⑥ 快照+落账 → ⑦ ops terminal → ⑧ enqueue →
  ⑨ 响应组装。
- 协议层(fastmcp in-process Client)与函数体内契约层是两道独立的门:
  `limit=5.5`/`None` 在协议层就被 Pydantic 拒绝,函数体从未执行,零账;
  `"5"`/`True` 被 Pydantic 隐式转型后才进函数体,按转型后的值判契约。
- reason 字段禁止携带查询原文/上游响应体。

Infinity 用本地 http.server 模拟(/models、/embeddings、/rerank,与
test_scorer.py 同款 stub);EverOS 用另一个独立 http.server 模拟
(/api/v1/memory/search);docker 经 monkeypatch `scorer._run_docker` 替身。
`ScoreWorker` 构造内部固定调用真实 `probe_passage.run_window_probe`(Task 7
冻结实现,无覆盖口子)——因此本文件几乎全部测试都要起 Infinity stub +
fake docker,复用本机已缓存的 pinned HF tokenizer 快照(同 test_scorer.py 的
既定假设)。PUBLIC 仓纪律:容器名/端口均为合成占位值,无真实拓扑字面量。
"""
from __future__ import annotations

import http.server as http_server_mod
import importlib
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from fastmcp.client import Client
from fastmcp.exceptions import ToolError

from everos_eval import probe_passage
from everos_mcp import ledger as ledger_mod
from everos_mcp import scorer

pytestmark = pytest.mark.slow  # 全文件依赖真实 pinned HF tokenizer 快照,标 slow 与既有约定一致


# ======================================================================
# Infinity HTTP stub(/models、/embeddings、/rerank)—— 与 test_scorer.py 同款
# ======================================================================

def _embed_vec(text: str) -> list:
    import hashlib

    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [(b + 1) / 256.0 for b in h[:8]]


def _rerank_score(query: str, doc: str) -> float:
    import hashlib

    h = hashlib.sha256((query + "\x00" + doc).encode("utf-8")).digest()
    return h[0] / 255.0


class _InfinityState:
    def __init__(self):
        self.requests: list[dict] = []
        self.models = [probe_passage.EMBED_MODEL_ID, probe_passage.RERANK_MODEL_ID]

    def record(self, method, path, payload=None):
        self.requests.append({"method": method, "path": path, "payload": payload})


class _InfinityHandler(http_server_mod.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        state = self.server.state
        state.record("GET", self.path)
        if self.path == "/models":
            self._json(200, {"data": [{"id": m} for m in state.models]})
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        state = self.server.state
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        state.record("POST", self.path, payload)
        if self.path == "/embeddings":
            texts = payload["input"]
            data = [{"index": i, "embedding": _embed_vec(t)} for i, t in enumerate(texts)]
            self._json(200, {"data": data})
            return
        if self.path == "/rerank":
            query = payload["query"]
            docs = payload["documents"]
            results = [
                {"index": i, "relevance_score": _rerank_score(query, d)}
                for i, d in enumerate(docs)
            ]
            self._json(200, {"results": results})
            return
        self.send_error(404)


class InfinityStub:
    def __init__(self):
        self.server = http_server_mod.ThreadingHTTPServer(("127.0.0.1", 0), _InfinityHandler)
        self.server.state = _InfinityState()
        self.state = self.server.state
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def infinity_stub():
    stub = InfinityStub()
    yield stub
    try:
        stub.shutdown()
    except Exception:
        pass


# ======================================================================
# docker stub(monkeypatch scorer._run_docker——与 test_scorer.py 同款)
# ======================================================================

class _CP:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _default_exec_output(tag: str) -> str:
    return (
        f"{'a' * 63}{tag[-1]}  /app/.cache/huggingface/hub/models--BAAI--bge-m3/blobs/config-{tag}\n"
        f"{'b' * 63}{tag[-1]}  /app/.cache/huggingface/hub/models--BAAI--bge-m3/blobs/weight-{tag}\n"
    )


class FakeDocker:
    def __init__(self):
        self.container_image = "sha256:" + "1" * 64
        self.config_image = "sha256:" + "2" * 64
        self.started_at = "2026-07-17T00:00:00.000000000Z"
        self.repo_digest = "example.invalid/cc-infinity@sha256:" + "3" * 64
        self.exec_output = _default_exec_output("v1")

    def run(self, args, timeout=30.0):
        if args[0] == "inspect":
            fmt = args[3]
            if fmt == "{{.Config.Image}}":
                return _CP(0, self.config_image, "")
            if fmt == "{{.Image}}":
                return _CP(0, self.container_image, "")
            if fmt == "{{.State.StartedAt}}":
                return _CP(0, self.started_at, "")
            return _CP(1, "", f"unsupported format {fmt}")
        if args[0] == "image" and args[1] == "inspect":
            return _CP(0, self.repo_digest, "")
        if args[0] == "exec":
            return _CP(0, self.exec_output, "")
        return _CP(1, "", f"unsupported docker args {args}")


@pytest.fixture
def fake_docker(monkeypatch):
    fd = FakeDocker()
    monkeypatch.setattr(scorer, "_run_docker", fd.run)
    return fd


# ======================================================================
# EverOS HTTP stub(/api/v1/memory/search)—— 独立于 Infinity 的另一台 stub
# ======================================================================

class _EverosState:
    def __init__(self):
        self.requests: list[dict] = []
        self.mode = "normal"  # normal | http_error | bad_json | redirect
        self.http_status = 500
        self.envelope = None  # 非 None 时覆盖默认空信封
        self.redirect_target = "http://127.0.0.1:1/dest"  # 从不会被真的请求到

    def record(self, payload):
        self.requests.append(payload)


class _EverosHandler(http_server_mod.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def do_POST(self):  # noqa: N802
        state = self.server.state
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        state.record(payload)

        if state.mode == "http_error":
            body = b"everos stub upstream broke"
            self.send_response(state.http_status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if state.mode == "bad_json":
            body = b"\xff\xfe not valid json at all"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if state.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", state.redirect_target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        envelope = state.envelope
        if envelope is None:
            envelope = {"request_id": "req-default", "data": {"agent_cases": [], "agent_skills": []}}
        body = json.dumps(envelope, allow_nan=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class EverosStub:
    def __init__(self):
        self.server = http_server_mod.ThreadingHTTPServer(("127.0.0.1", 0), _EverosHandler)
        self.server.state = _EverosState()
        self.state = self.server.state
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def everos_stub():
    stub = EverosStub()
    yield stub
    try:
        stub.shutdown()
    except Exception:
        pass


def _closed_port_url() -> str:
    """绑一个端口再立刻关掉——connect 到这个 URL 会立刻 ECONNREFUSED(URLError,
    非 HTTPError),不用真的等 10s socket timeout 就能触发"连接级失败"分支。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ======================================================================
# Config / env 组装(与 test_scorer.py 同款,追加 everos_stub 独立 base_url)
# ======================================================================

def _build_env(tmp_path: Path, everos_base: str, infinity_base: str, *,
                container: str = "test-infinity", expect_empty: bool = False,
                traffic_class: str | None = None) -> tuple[dict, Path]:
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(exist_ok=True)
    ledger_dir.chmod(0o700)
    instance_dir = tmp_path / "instance"
    (instance_dir / ".cases").mkdir(parents=True, exist_ok=True)
    (instance_dir / "skills" / "demo-skill").mkdir(parents=True, exist_ok=True)
    (instance_dir / "skills" / "demo-skill" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    case_file = instance_dir / ".cases" / "agent_case-2026-07-17.md"
    case_file.write_text("---\nentry_count: 1\n---\n# cases\n", encoding="utf-8")
    pin_file = tmp_path / "PIN"
    pin_file.write_text("git_sha=deadbeef\nfreeze_hash=cafef00d\n", encoding="utf-8")

    env = {
        "EVEROS_MCP_PORT": "1",
        "EVEROS_MCP_TOKEN": "test-token",
        "EVEROS_BASE_URL": everos_base,
        "EVEROS_AGENT_ID": "test-agent",
        "INFINITY_BASE": infinity_base,
        "SHADOW_LEDGER_DIR": str(ledger_dir),
        "EVEROS_EMBED_MODEL": probe_passage.EMBED_MODEL_ID,
        "EVEROS_RERANK_MODEL": probe_passage.RERANK_MODEL_ID,
        "EVEROS_PIN_FILE": str(pin_file),
        "EVEROS_INSTANCE_DIR": str(instance_dir),
        "INFINITY_CONTAINER": container,
    }
    if expect_empty:
        env["EVEROS_MCP_EXPECT_EMPTY"] = "1"
    if traffic_class:
        env["SHADOW_TRAFFIC_CLASS"] = traffic_class
    return env, ledger_dir


def _apply_env(monkeypatch, env: dict) -> None:
    for k in list(os.environ):
        if k.startswith(("EVEROS_", "SHADOW_", "INFINITY_", "TELEGRAM_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


# ======================================================================
# 模块 reload 夹具:每个测试拿到一份干净的 everos_mcp.server(重新执行模块
# 顶层代码,含 bearer fail-fast + 新 FastMCP 实例),测试结束尽力回收
# bootstrap() 产出的后台线程/flock fd。
# ======================================================================

@pytest.fixture
def fresh_server(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("EVEROS_", "SHADOW_", "INFINITY_", "TELEGRAM_")):
            monkeypatch.delenv(k, raising=False)

    holder: dict = {"mod": None}

    def _reload():
        import everos_mcp.server as mod
        importlib.reload(mod)
        holder["mod"] = mod
        return mod

    holder["reload"] = _reload
    yield holder

    mod = holder.get("mod")
    if mod is not None and mod._STATE is not None:
        state = mod._STATE
        state.watchdog_stop.set()
        try:
            state.worker.close(drain=False)
        except Exception:
            pass
        try:
            state.ledger.close(drain=False)
        except Exception:
            pass


def _wait_for_scored(ledger_dir, rid, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows, _ = ledger_mod.iter_rows(ledger_dir, "scored")
        matches = [r for r in rows if r.get("rid") == rid]
        if matches:
            return matches
        time.sleep(0.05)
    raise AssertionError(f"等 rid={rid!r} 的 scored 行超时({timeout}s)")


def _ops_rows_for(ledger_dir, rid):
    rows, _ = ledger_mod.iter_rows(ledger_dir, "ops")
    return [r for r in rows if r.get("rid") == rid]


def _accepted_rows_for(ledger_dir, rid):
    rows, _ = ledger_mod.iter_rows(ledger_dir, "accepted")
    return [r for r in rows if r.get("rid") == rid]


def _full_envelope_two_cases_two_skills():
    return {
        "request_id": "req-full",
        "data": {
            "agent_cases": [
                {"id": "ac_1", "score": 0.9, "task_intent": "调研 X 的技术方案",
                 "approach": "先读 spec 再对照实现"},
                {"id": "ac_2", "score": 0.5, "task_intent": "修 Y 的 bug",
                 "approach": "复现后二分定位"},
            ],
            "agent_skills": [
                {"id": "sk_1", "score": 0.8, "name": "调研技能",
                 "description": "先框架后细节"},
                {"id": "sk_2", "score": 0.3, "name": "修 bug 技能",
                 "description": "先复现后定位"},
            ],
        },
    }


# ======================================================================
# 1. 全链命中(hit):stub 2 case + 2 skill → 4 卡 skill-first 交错序;
#    ops/accepted 行齐;worker 收到 enqueue 并产出 scored 行。
# ======================================================================

def test_full_hit_chain_skill_first_interleave_and_worker_enqueued(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    everos_stub.state.envelope = _full_envelope_two_cases_two_skills()
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)

    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("调研任务", 5)

    assert result["status"] == "hit"
    assert result["reason"]
    assert "调研任务" not in result["reason"]  # reason 禁止携带查询原文
    cards = result["cards"]
    assert [c["id"] for c in cards] == ["sk_1", "ac_1", "sk_2", "ac_2"]  # skill-first 交错
    for c in cards:
        assert set(c.keys()) == {"id", "card_type", "truncated", "payload"}

    meta = result["meta"]
    assert meta["guard_mode"] == "shadow"
    assert meta["raw_returned"] == 4
    assert meta["error_code"] is None
    rid = meta["mcp_request_id"]

    ops_rows = _ops_rows_for(ledger_dir, rid)
    assert {r["kind"] for r in ops_rows} == {"started", "terminal"}
    terminal = next(r for r in ops_rows if r["kind"] == "terminal")
    assert terminal["effective_status"] == "hit"

    accepted_rows = _accepted_rows_for(ledger_dir, rid)
    assert len(accepted_rows) == 1
    assert accepted_rows[0]["stage"] == "hit"
    assert len(accepted_rows[0]["candidates"]) == 4
    # P2(R4 #9):returned_ids 是 (card_type, card_id) 序对——JSON 无 tuple,
    # 落盘编码为 [card_type, card_id] 两元素列表,顺序与响应 cards 一致
    # (skill-first 交错序)。
    assert accepted_rows[0]["returned_ids"] == [
        ["agent_skill", "sk_1"], ["agent_case", "ac_1"],
        ["agent_skill", "sk_2"], ["agent_case", "ac_2"],
    ]

    scored_rows = _wait_for_scored(ledger_dir, rid)
    assert len(scored_rows) == 1
    assert scored_rows[0]["status"] == "ok"


def test_full_hit_chain_limit_truncates_returned_cards(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    everos_stub.state.envelope = _full_envelope_two_cases_two_skills()
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)

    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("调研任务二", 2)
    assert result["status"] == "hit"
    assert [c["id"] for c in result["cards"]] == ["sk_1", "ac_1"]
    assert result["meta"]["raw_returned"] == 4  # raw_returned 记全量候选,不受 limit 截断影响


def _full_envelope_three_cases_three_skills():
    return {
        "request_id": "req-full-6",
        "data": {
            "agent_cases": [
                {"id": f"ac_{i}", "score": 0.9 - i * 0.1,
                 "task_intent": f"任务意图 {i}", "approach": f"方案 {i}"}
                for i in range(3)
            ],
            "agent_skills": [
                {"id": f"sk_{i}", "score": 0.8 - i * 0.1,
                 "name": f"技能 {i}", "description": f"描述 {i}"}
                for i in range(3)
            ],
        },
    }


def test_accepted_candidates_cover_all_raw_candidates_not_just_returned(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    """P0:accepted 行的 candidates(以及由此驱动的打分/复标)必须覆盖**全部
    原始候选**(top_k=20/类型,非仅返回的 limit 张),不能只从 `returned`
    (被 limit 截断后的交错序)构建——否则未返回的候选永远进不了影子账,
    标定阶段就少了绝大多数数据。3 case + 3 skill、limit=2 → accepted 行必须
    有 6 条候选(each 带 payload_sha/passage_sha/truncated),returned_ids
    只有 2 个(响应确实被 limit 截断,不受影响)。"""
    everos_stub.state.envelope = _full_envelope_three_cases_three_skills()
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)

    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("覆盖全部候选任务", 2)
    assert result["status"] == "hit"
    assert len(result["cards"]) == 2  # 响应仍按 limit 截断

    rid = result["meta"]["mcp_request_id"]
    accepted = _accepted_rows_for(ledger_dir, rid)[0]
    candidates = accepted["candidates"]
    assert len(candidates) == 6  # 全部原始候选(3 case + 3 skill),不是仅返回的 2 张
    assert {c["card_id"] for c in candidates} == {f"ac_{i}" for i in range(3)} | {f"sk_{i}" for i in range(3)}
    for c in candidates:
        assert isinstance(c["payload_sha"], str) and c["payload_sha"]
        assert isinstance(c["passage_sha"], str) and c["passage_sha"]
        assert isinstance(c["truncated"], bool)

    assert len(accepted["returned_ids"]) == 2

    scored_rows = _wait_for_scored(ledger_dir, rid)
    assert len(scored_rows) == 1
    row = scored_rows[0]
    assert row["status"] == "ok"
    assert set(row["per_card"].keys()) == {
        f"agent_case:ac_{i}" for i in range(3)
    } | {f"agent_skill:sk_{i}" for i in range(3)}


# ======================================================================
# 1b (P2/R4 #4). everos_pin 必须逐请求重读,不能只在 bootstrap 时算一次
# 就长期 boot-cache——PIN 文件是上游 everos-prod 进程的属性,会在其重部署
# 时变化。
# ======================================================================

def test_accepted_row_config_fp_picks_up_pin_file_swap_mid_run(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    """PIN 文件在两次请求之间被替换内容(模拟 everos-prod 重部署)——第二次
    请求的 accepted 行必须携带新 pin,不是 bootstrap 时读到的旧值。"""
    everos_stub.state.envelope = _full_envelope_two_cases_two_skills()
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    pin_path = Path(env["EVEROS_PIN_FILE"])
    _apply_env(monkeypatch, env)

    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result1 = server_mod.everos_search("查任务一", 5)
    rid1 = result1["meta"]["mcp_request_id"]
    accepted1 = _accepted_rows_for(ledger_dir, rid1)[0]
    assert accepted1["config_fp"]["everos_pin"] == "git_sha=deadbeef\nfreeze_hash=cafef00d\n"

    new_mtime = pin_path.stat().st_mtime + 5
    pin_path.write_text("git_sha=newsha\nfreeze_hash=newfreeze\n", encoding="utf-8")
    os.utime(pin_path, (new_mtime, new_mtime))

    result2 = server_mod.everos_search("查任务二", 5)
    rid2 = result2["meta"]["mcp_request_id"]
    accepted2 = _accepted_rows_for(ledger_dir, rid2)[0]
    assert accepted2["config_fp"]["everos_pin"] == "git_sha=newsha\nfreeze_hash=newfreeze\n"
    # 静态字段(server_git_sha 等)不受影响,两次请求应该一致
    assert accepted1["config_fp"]["server_git_sha"] == accepted2["config_fp"]["server_git_sha"]
    assert accepted1["config_fp"]["agent_id"] == accepted2["config_fp"]["agent_id"]


def test_pin_file_missing_at_request_time_fails_closed_with_internal_error(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    """PIN 文件在启动后、请求发生前被删除——这是我们自己的配置层故障,不是
    上游 EverOS 响应异常,必须 fail-closed 返回 error_code="internal"(见
    `_finish_config_fp_broken` 文档,已选定并记录这一判断),而不是让异常
    未捕获地向上冒。ops 流仍必须有 started+terminal(error)两行。"""
    everos_stub.state.envelope = _full_envelope_two_cases_two_skills()
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    pin_path = Path(env["EVEROS_PIN_FILE"])
    _apply_env(monkeypatch, env)

    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    pin_path.unlink()

    result = server_mod.everos_search("查任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "internal"
    assert result["meta"]["retryable"] is False
    assert result["cards"] == []

    rid = result["meta"]["mcp_request_id"]
    ops_rows = _ops_rows_for(ledger_dir, rid)
    assert {r["kind"] for r in ops_rows} == {"started", "terminal"}
    terminal = next(r for r in ops_rows if r["kind"] == "terminal")
    assert terminal["effective_status"] == "error"
    assert terminal["error_code"] == "internal"
    # 没有可提交的 accepted 行(config_fp 还没组好,契约门都没跑到)。
    assert _accepted_rows_for(ledger_dir, rid) == []


def test_pin_file_unreadable_mid_run_fails_closed_with_internal_error(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    """P2:PIN 文件在启动后依然存在,但请求发生时不可读(`chmod 000`,权限
    坏掉/属主变化等运维事故的最小复现)——`PinFileCache.read()` 此前只捕获
    `stat()` 失败,`read_text()` 抛出的 `PermissionError` 会原样冒出未捕获
    异常,而不是走 `_finish_config_fp_broken` 的 fail-closed internal 错误
    路径。修复后,这类失败必须和"文件缺失"同一处置:返回 error 响应(不是
    原始异常向上抛),ops 流仍必须有 started+terminal(error)两行。

    `bootstrap()` 会先读一次 PIN 并按 mtime/size 缓存——仅 `chmod` 不改变
    这两者,请求时会直接命中缓存、根本走不到 `read_text()`。这里先换内容
    (顺带拨动 mtime)让缓存必然失效,再 `chmod(0o000)`,确保请求时刻真的
    会尝试重新 `read_text()` 并撞上权限失败。"""
    everos_stub.state.envelope = _full_envelope_two_cases_two_skills()
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    pin_path = Path(env["EVEROS_PIN_FILE"])
    _apply_env(monkeypatch, env)

    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    new_mtime = pin_path.stat().st_mtime + 5
    pin_path.write_text("git_sha=newsha\nfreeze_hash=newfreeze\n", encoding="utf-8")
    os.utime(pin_path, (new_mtime, new_mtime))
    pin_path.chmod(0o000)
    try:
        result = server_mod.everos_search("查任务", 5)
    finally:
        pin_path.chmod(0o600)

    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "internal"
    assert result["meta"]["retryable"] is False
    assert result["cards"] == []

    rid = result["meta"]["mcp_request_id"]
    ops_rows = _ops_rows_for(ledger_dir, rid)
    assert {r["kind"] for r in ops_rows} == {"started", "terminal"}
    terminal = next(r for r in ops_rows if r["kind"] == "terminal")
    assert terminal["effective_status"] == "error"
    assert terminal["error_code"] == "internal"
    assert _accepted_rows_for(ledger_dir, rid) == []


# ======================================================================
# 2. 重放测试:仅凭 accepted 行 + 快照复算 cards,与实际响应逐字段一致。
# ======================================================================

def test_replay_from_accepted_row_and_snapshots_matches_response(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    everos_stub.state.envelope = _full_envelope_two_cases_two_skills()
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)

    server_mod = fresh_server["reload"]()
    state = server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("重放测试任务", 5)
    rid = result["meta"]["mcp_request_id"]
    accepted = _accepted_rows_for(ledger_dir, rid)[0]

    # P0:accepted["candidates"] 现在是全部原始候选(cases 全序 + skills 全序),
    # 不再是 `returned` 的交错序——复算响应必须按 `returned_ids`(响应实际的
    # skill-first 交错序 + limit 截断)过滤/排序,不能假设两者顺序/成员一致。
    # P2(R4 #9):returned_ids 现在是 [card_type, card_id] 序对,不是裸 card_id
    # ——JSON 无 tuple,查表键改用 (card_type, card_id) 元组。
    by_id = {(c["card_type"], c["card_id"]): c for c in accepted["candidates"]}
    replayed_cards = []
    for card_type, card_id in accepted["returned_ids"]:
        c = by_id[(card_type, card_id)]
        payload = json.loads(state.blobstore.get(c["payload_sha"]))
        replayed_cards.append({
            "id": c["card_id"], "card_type": c["card_type"],
            "truncated": c["truncated"], "payload": payload,
        })

    assert replayed_cards == result["cards"]


def test_replay_from_accepted_row_matches_response_when_candidates_exceed_returned(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    """P0 场景下 accepted 行的 candidates(6)比响应 cards(limit=2)多——重放
    必须先按 `returned_ids` 过滤/排序 accepted 候选,再逐字段匹配响应,而不能
    再假设"accepted 候选 == 响应 cards"(这条假设是 P0 修复前的旧世界观)。"""
    everos_stub.state.envelope = _full_envelope_three_cases_three_skills()
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)

    server_mod = fresh_server["reload"]()
    state = server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("重放测试任务二", 2)
    rid = result["meta"]["mcp_request_id"]
    accepted = _accepted_rows_for(ledger_dir, rid)[0]
    assert len(accepted["candidates"]) == 6
    assert len(result["cards"]) == 2

    by_id = {(c["card_type"], c["card_id"]): c for c in accepted["candidates"]}
    replayed_cards = []
    for card_type, card_id in accepted["returned_ids"]:
        c = by_id[(card_type, card_id)]
        payload = json.loads(state.blobstore.get(c["payload_sha"]))
        replayed_cards.append({
            "id": c["card_id"], "card_type": c["card_type"],
            "truncated": c["truncated"], "payload": payload,
        })

    assert replayed_cards == result["cards"]


# ======================================================================
# 3. 协议层(fastmcp in-process client):类型拒绝 vs 隐式转型
# ======================================================================

async def _call_via_client(server_mod, task, limit):
    client = Client(server_mod.mcp)
    async with client:
        return await client.call_tool("everos_search", {"task": task, "limit": limit})


@pytest.mark.parametrize("bad_limit", [5.5, None])
def test_protocol_level_invalid_limit_rejected_before_function_body_no_ledger_rows(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub, bad_limit,
):
    import asyncio

    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    ops_before, _ = ledger_mod.iter_rows(ledger_dir, "ops")
    with pytest.raises(ToolError):
        asyncio.run(_call_via_client(server_mod, "x", bad_limit))
    ops_after, _ = ledger_mod.iter_rows(ledger_dir, "ops")
    assert ops_after == ops_before  # 协议级拒绝根本没进函数体,零账


@pytest.mark.parametrize("coerced_limit,expected_count", [("5", 4), (True, 1)])
def test_protocol_level_implicit_coercion_judged_post_coercion(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
    coerced_limit, expected_count,
):
    import asyncio

    everos_stub.state.envelope = _full_envelope_two_cases_two_skills()
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    res = asyncio.run(_call_via_client(server_mod, "任务x", coerced_limit))
    assert res.is_error is False
    assert res.data["status"] == "hit"
    assert len(res.data["cards"]) == expected_count


# ======================================================================
# 4. 契约违规 → error + ops 行 + accepted stage=contract_reject 无 candidates 键
# ======================================================================

def test_contract_violation_linebreak_records_contract_reject_without_candidates_key(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("fix bug\n", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "task_has_linebreak"
    assert result["meta"]["retryable"] is False
    assert result["cards"] == []
    assert "fix bug" not in result["reason"]

    rid = result["meta"]["mcp_request_id"]
    ops_rows = _ops_rows_for(ledger_dir, rid)
    assert {r["kind"] for r in ops_rows} == {"started", "terminal"}
    terminal = next(r for r in ops_rows if r["kind"] == "terminal")
    assert terminal["effective_status"] == "error"
    assert terminal["error_code"] == "task_has_linebreak"

    accepted_rows = _accepted_rows_for(ledger_dir, rid)
    assert len(accepted_rows) == 1
    assert accepted_rows[0]["stage"] == "contract_reject"
    assert "candidates" not in accepted_rows[0]
    assert accepted_rows[0]["query"] is None


@pytest.mark.parametrize("bad_limit,code", [(0, "limit_out_of_range"), (6, "limit_out_of_range")])
def test_contract_violation_limit_out_of_range(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub, bad_limit, code,
):
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("正常任务", bad_limit)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == code


# ======================================================================
# 5. EverOS 失败矩阵:500 / 404 / 连接超时 / 重复 id / NaN native score
# ======================================================================

def test_upstream_5xx_maps_to_http_error_retryable(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    everos_stub.state.mode = "http_error"
    everos_stub.state.http_status = 500
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("查任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "everos_http_error"
    assert result["meta"]["retryable"] is True

    rid = result["meta"]["mcp_request_id"]
    accepted = _accepted_rows_for(ledger_dir, rid)[0]
    assert accepted["stage"] == "upstream_fail"
    assert "candidates" not in accepted


def test_upstream_4xx_maps_to_http_error_non_retryable(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    everos_stub.state.mode = "http_error"
    everos_stub.state.http_status = 404
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("查任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "everos_http_error"
    assert result["meta"]["retryable"] is False


def test_upstream_connection_level_failure_maps_to_timeout_retryable(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    dead_url = _closed_port_url()
    env, _ = _build_env(tmp_path, dead_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("查任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "everos_timeout"
    assert result["meta"]["retryable"] is True


def test_upstream_duplicate_card_id_maps_to_bad_response(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    everos_stub.state.envelope = {
        "request_id": "req-dup",
        "data": {
            "agent_cases": [{"id": "dup_1", "score": 0.5, "task_intent": "a", "approach": "b"}],
            "agent_skills": [{"id": "dup_1", "score": 0.4, "name": "a", "description": "b"}],
        },
    }
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("查任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "everos_bad_response"
    assert result["meta"]["retryable"] is False


def test_upstream_nan_native_score_maps_to_bad_response(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    everos_stub.state.envelope = {
        "request_id": "req-nan",
        "data": {
            "agent_cases": [{"id": "ac_nan", "score": float("nan"), "task_intent": "a", "approach": "b"}],
            "agent_skills": [],
        },
    }
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("查任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "everos_bad_response"


def test_upstream_malformed_card_field_type_maps_to_bad_response_not_internal(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    """P1f/P2a:候选 payload 白名单字段类型非法(如 `task_intent` 是 list 不是
    str)——此前这类畸形字段能滑过 `normalize_candidates` 的校验,一路带到
    `build_snapshots`/`build_passage` 阶段才炸 `TypeError`("\\n".join 需要
    str 元素),被外层 broad except 误判成 internal。schema 校验必须在
    `normalize_candidates` 里就地拦截,落 everos_bad_response(失败矩阵:
    响应坏),不是 internal。"""
    everos_stub.state.envelope = {
        "request_id": "req-badfield",
        "data": {
            "agent_cases": [
                {"id": "ac_bad", "score": 0.5, "task_intent": ["not", "a", "string"], "approach": "b"},
            ],
            "agent_skills": [],
        },
    }
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("查任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "everos_bad_response"
    assert result["meta"]["retryable"] is False


def test_upstream_redirect_maps_to_bad_response_not_internal(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    """final-review M9.1:EverOS 返回 30x 重定向必须落 everos_bad_response(spec
    §8-1:按上游故障处理),不能落在外层 broad except 的 internal 分支——回归
    此前 `http.RedirectRefused` 未被显式捕获、悄悄滑进 internal 的偏差。"""
    everos_stub.state.mode = "redirect"
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("查任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "everos_bad_response"
    assert result["meta"]["retryable"] is False


def test_upstream_non_json_response_maps_to_bad_response_separately_from_internal(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    """非 JSON 响应必须落 everos_bad_response,不能被外层 broad except 误判成
    internal——单独测试,不与 http_error/normalize 违规共用一个用例。"""
    everos_stub.state.mode = "bad_json"
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("查任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "everos_bad_response"
    assert result["meta"]["retryable"] is False


def test_upstream_oversized_2xx_body_maps_to_bad_response_not_internal(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    """P1c:响应体超过 `http.MAX_RESPONSE_BYTES`(`http.ResponseTooLarge`)此前
    只被 `http.BadJson` 一种异常捕获,`ResponseTooLarge` 会滑过 `upstream.search`
    的 except 分支、被 `_handle_search` 最外层 broad except 误判成 internal
    ——必须同 BadJson 一样落 everos_bad_response。monkeypatch 调小上限,避免
    真的构造超大 fixture body(与 test_http.py/test_upstream.py 同一约定)。"""
    from everos_mcp import http as http_mod

    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    # 启动序里的 /models 探针必须先用正常上限跑完,只在真正调用 everos_search
    # 时才把上限调小——否则 bootstrap 自身对 Infinity 的 /models 探针也会被
    # 误伤而拿不到窗口探测结果。
    monkeypatch.setattr(http_mod, "MAX_RESPONSE_BYTES", 8)  # 正常空信封响应体也会超过 8 字节
    result = server_mod.everos_search("查任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "everos_bad_response"
    assert result["meta"]["retryable"] is False


def test_upstream_top_level_json_array_maps_to_bad_response_not_internal(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    """EverOS 响应是合法 JSON 但顶层是数组(非对象)——`normalize_candidates` 之前
    会直接 `resp.get(...)` 摔 AttributeError,被外层 broad except 误判成
    internal。失败矩阵要求畸形信封一律 everos_bad_response。"""
    everos_stub.state.envelope = [1, 2, 3]
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("查任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "everos_bad_response"
    assert result["meta"]["retryable"] is False


# ======================================================================
# 6. 空结果 → abstain_empty
# ======================================================================

def test_empty_upstream_result_is_abstain_empty_not_error(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    result = server_mod.everos_search("查一个库里没有的东西", 5)
    assert result["status"] == "abstain_empty"
    assert result["cards"] == []
    assert result["meta"]["error_code"] is None
    assert "库存为空" in result["reason"]

    rid = result["meta"]["mcp_request_id"]
    terminal = next(r for r in _ops_rows_for(ledger_dir, rid) if r["kind"] == "terminal")
    assert terminal["effective_status"] == "abstain_empty"
    assert "error_code" not in terminal


# ======================================================================
# 7. 启动自检探针:空库耗尽重试 → SystemExit(87);expect_empty=1 跳过
# ======================================================================

def test_startup_probe_exhausted_retries_raises_system_exit_87(
    tmp_path, monkeypatch, everos_stub,
):
    env, _ = _build_env(tmp_path, everos_stub.base_url, "http://127.0.0.1:1")
    _apply_env(monkeypatch, env)
    import everos_mcp.server as server_mod
    importlib.reload(server_mod)

    cfg = server_mod.config_mod.load()
    with pytest.raises(SystemExit) as exc_info:
        server_mod._startup_probe(
            cfg, max_attempts=2, budget_seconds=0.02, sleep_fn=lambda s: None,
        )
    assert exc_info.value.code == 87


def test_startup_probe_succeeds_when_upstream_eventually_non_empty(
    tmp_path, monkeypatch, everos_stub,
):
    env, _ = _build_env(tmp_path, everos_stub.base_url, "http://127.0.0.1:1")
    _apply_env(monkeypatch, env)
    import everos_mcp.server as server_mod
    importlib.reload(server_mod)

    call_count = {"n": 0}

    def flaky_search(cfg, query):
        call_count["n"] += 1
        if call_count["n"] < 2:
            return {"request_id": "r", "data": {"agent_cases": [], "agent_skills": []}}
        return {"request_id": "r", "data": {
            "agent_cases": [{"id": "ac_1", "score": 0.5, "task_intent": "a", "approach": "b"}],
            "agent_skills": [],
        }}

    monkeypatch.setattr(server_mod.upstream, "search", flaky_search)
    cfg = server_mod.config_mod.load()
    server_mod._startup_probe(cfg, max_attempts=3, budget_seconds=0.02, sleep_fn=lambda s: None)
    assert call_count["n"] == 2


def test_startup_probe_skipped_when_expect_empty(tmp_path, monkeypatch, everos_stub):
    env, _ = _build_env(tmp_path, everos_stub.base_url, "http://127.0.0.1:1", expect_empty=True)
    _apply_env(monkeypatch, env)
    import everos_mcp.server as server_mod
    importlib.reload(server_mod)

    cfg = server_mod.config_mod.load()
    assert cfg.expect_empty is True

    def _boom(*a, **kw):
        raise AssertionError("expect_empty=1 时不应该真的发起探针查询")

    monkeypatch.setattr(server_mod.upstream, "search", _boom)
    server_mod._startup_probe(cfg, max_attempts=3, budget_seconds=0.02, sleep_fn=lambda s: None)


# ======================================================================
# 8. checkpoint overdue 短路
# ======================================================================

def test_checkpoint_overdue_short_circuits_before_upstream(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    state = server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    now = time.time()
    meta = {
        "launched_ts": now - 40 * 86400,
        "due_since": now - 8 * 86400,
        "reviews": [],
    }
    state.checkpoint.meta_path.write_text(json.dumps(meta), encoding="utf-8")

    def _boom(*a, **kw):
        raise AssertionError("overdue 时不应该发起 upstream 调用")

    monkeypatch.setattr(server_mod.upstream, "search", _boom)

    result = server_mod.everos_search("被拦截的任务", 5)
    assert result["status"] == "error"
    assert result["meta"]["error_code"] == "review_overdue"
    assert result["meta"]["retryable"] is False
    assert "被拦截的任务" not in result["reason"]

    rid = result["meta"]["mcp_request_id"]
    accepted = _accepted_rows_for(ledger_dir, rid)[0]
    assert accepted["stage"] == "gated"
    assert accepted["query"] == "被拦截的任务"  # gated 行本身要求记入(已 strip)
    assert "candidates" not in accepted


# ======================================================================
# 9. ops-started 写失败 → os._exit(86)(monkeypatch _hard_exit 验证退出码)
# ======================================================================

def test_ops_started_write_failure_triggers_hard_exit_86(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    state = server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    exit_calls = []
    monkeypatch.setattr(server_mod, "_hard_exit", lambda code: exit_calls.append(code))

    def _broken_submit(row, timeout=5.0):
        raise RuntimeError("模拟 ops started fsync 失败")

    monkeypatch.setattr(state.ledger.ops, "submit", _broken_submit)

    server_mod.everos_search("任意任务", 5)
    assert exit_calls == [86]


# ======================================================================
# 10. watchdog:writer/worker 死→重启一次→再死 unit fail;orphan/checkpoint/磁盘告警
# ======================================================================

def test_watchdog_dead_writer_restarts_once_then_hard_exits(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    state = server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    monkeypatch.setattr(state.ledger.ops, "alive", lambda: False)
    restart_calls = []
    monkeypatch.setattr(server_mod, "_restart_ledger_writer", lambda st, name: restart_calls.append(name))

    exit_calls = []
    monkeypatch.setattr(server_mod, "_hard_exit", lambda code: exit_calls.append(code))

    server_mod._watchdog_pass(state, now=time.time())
    assert restart_calls == ["ops"]
    assert exit_calls == []

    server_mod._watchdog_pass(state, now=time.time())
    assert exit_calls == [1]


def test_watchdog_orphan_alert_fires_for_stale_score_eligible_row(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    state = server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    rid = "orphan-rid"
    old_ts = time.time() - 25 * 3600
    state.ledger.ops.submit(ledger_mod.ops_started(rid, "real"))
    state.ledger.ops.submit(ledger_mod.ops_terminal(rid, "hit"))
    candidate = {
        "card_id": "c1", "card_type": "agent_case", "source_rank": 0,
        "native_score": 0.9, "payload_sha": "a" * 64, "passage_sha": "b" * 64, "truncated": False,
    }
    accepted = ledger_mod.accepted_row(
        "hit", rid, old_ts, "real", query="q", q_len=1, everos_rid="er-1",
        candidates=[candidate], returned_ids=["c1"], search_ms=1.0, pre_commit_ms=1.0, config_fp={},
    )
    state.ledger.accepted.submit(accepted)

    alerts = []
    monkeypatch.setattr(server_mod, "_alert", lambda msg: alerts.append(msg))
    server_mod._watchdog_pass(state, now=time.time())
    assert any("orphan" in m for m in alerts)


def test_watchdog_alert_content_never_contains_query_text(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    """告警内容零明文——即便 orphan 查询里带敏感 query 文本,alert 消息也不能
    出现它。"""
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    state = server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    rid = "orphan-secret"
    secret_query = "一个不该出现在告警里的敏感查询原文"
    old_ts = time.time() - 25 * 3600
    state.ledger.ops.submit(ledger_mod.ops_started(rid, "real"))
    state.ledger.ops.submit(ledger_mod.ops_terminal(rid, "hit"))
    candidate = {
        "card_id": "c1", "card_type": "agent_case", "source_rank": 0,
        "native_score": 0.9, "payload_sha": "a" * 64, "passage_sha": "b" * 64, "truncated": False,
    }
    accepted = ledger_mod.accepted_row(
        "hit", rid, old_ts, "real", query=secret_query, q_len=len(secret_query), everos_rid="er-1",
        candidates=[candidate], returned_ids=["c1"], search_ms=1.0, pre_commit_ms=1.0, config_fp={},
    )
    state.ledger.accepted.submit(accepted)

    captured = []
    monkeypatch.setattr(server_mod._LOG, "critical", lambda msg: captured.append(msg))
    server_mod._watchdog_pass(state, now=time.time())
    assert captured  # 至少触发了一条告警(orphan)
    assert all(secret_query not in m for m in captured)


def test_watchdog_disk_usage_alert(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    state = server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    monkeypatch.setattr(server_mod, "_dir_usage_bytes", lambda root: 6 * 1024 * 1024 * 1024)
    alerts = []
    monkeypatch.setattr(server_mod, "_alert", lambda msg: alerts.append(msg))
    server_mod._watchdog_pass(state, now=time.time())
    assert any("用量" in m for m in alerts)


def test_watchdog_checkpoint_due_alert(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    env, _ = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    state = server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    now = time.time()
    meta = {"launched_ts": now - 31 * 86400, "due_since": None, "reviews": []}
    state.checkpoint.meta_path.write_text(json.dumps(meta), encoding="utf-8")

    alerts = []
    monkeypatch.setattr(server_mod, "_alert", lambda msg: alerts.append(msg))
    server_mod._watchdog_pass(state, now=now)
    assert any("checkpoint state=due" in m for m in alerts)


# ======================================================================
# 11. bootstrap 启动序:Checkpoint 正确接了 earliest_ledger_ts;bearer fail-fast
# ======================================================================

def test_bootstrap_wires_earliest_ledger_ts_into_checkpoint(
    tmp_path, monkeypatch, fresh_server, infinity_stub, fake_docker, everos_stub,
):
    env, ledger_dir = _build_env(tmp_path, everos_stub.base_url, infinity_stub.base_url)
    _apply_env(monkeypatch, env)
    server_mod = fresh_server["reload"]()
    state = server_mod.bootstrap(skip_probe=True, start_watchdog=False)

    meta = json.loads(state.checkpoint.meta_path.read_text(encoding="utf-8"))
    assert meta["launched_ts"] is not None

    # 再跑一次搜索产生一条 real 的 ops started 行,验证第二次 bootstrap(模拟
    # 重启)能正常用 earliest_ledger_ts 通过 init_or_load 校验,不 fail-closed。
    server_mod.everos_search("热身查询", 5)
    state.worker.close(drain=False)
    state.ledger.close(drain=False)

    server_mod2 = fresh_server["reload"]()
    server_mod2.bootstrap(skip_probe=True, start_watchdog=False)  # 不应抛 CheckpointCorrupt


def test_module_import_bearer_fail_fast_when_token_missing(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("EVEROS_", "SHADOW_", "INFINITY_")):
            monkeypatch.delenv(k, raising=False)
    import everos_mcp.server as server_mod

    with pytest.raises(RuntimeError):
        importlib.reload(server_mod)

    # 现场清理:恢复一个可用 token,把模块 reload 回可用状态,避免污染后续测试
    monkeypatch.setenv("EVEROS_MCP_TOKEN", "test-token")
    importlib.reload(server_mod)
