"""scorer.py 的测试(P4 Task 7:打分 worker + pins + reconciliation)。

固定纪律(见 everos_mcp/scorer.py 顶部文档字符串,均为简报冻结项):
- collect_pins 键集与 materialize.PIN_KEYS 精确一致,任一子项失败 raise。
- 打分调用全在 worker 自己的后台线程;HTTP 一律经 http.post_json 注入进
  probe_scores.embed/rerank。
- 卡向量缓存键 = (passage_sha, model, artifact_fp),随 artifact_fp 变化天然
  失效(marker 双取发现容器漂移时原子重建整份 pin bundle)。
- reconcile 是纯函数,注入打分 primitive(通常是 ScoreWorker._score_once),
  anti-join + 阈值逻辑可独立于真实打分单测。

Infinity 用本地 `http.server`(ThreadingHTTPServer)模拟 `/models`/`/embeddings`/
`/rerank`;docker 经 `monkeypatch` 直接替换 `scorer._run_docker`(简报允许的两种
stub 方式之一,零真实 docker 依赖)。tokenizer/git/uv.lock 用本机真实文件
(本仓真实 git 仓库 + `uv.lock` + 已缓存的 pinned HF tokenizer 快照,同
everos_eval 既有测试的既定假设)。PUBLIC 仓纪律:容器名/端口均为合成占位值
(`test-infinity`、`127.0.0.1:0` 临时端口),无真实拓扑字面量。
"""
from __future__ import annotations

import hashlib
import http.server as http_server_mod
import json
import os
import threading
import time
from collections import namedtuple
from pathlib import Path

import pytest

from everos_eval import probe_passage
from everos_mcp import blobstore as blobstore_mod
from everos_mcp import config as config_mod
from everos_mcp import ledger as ledger_mod
from everos_mcp import materialize
from everos_mcp import scorer

_CP = namedtuple("_CP", ["returncode", "stdout", "stderr"])


# ======================================================================
# Infinity HTTP stub(/models, /embeddings, /rerank)
# ======================================================================

def _embed_vec(text: str) -> list:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [(b + 1) / 256.0 for b in h[:8]]  # 8 维,全分量非零(避免零向量 cosine 报错)


def _rerank_score(query: str, doc: str) -> float:
    h = hashlib.sha256((query + "\x00" + doc).encode("utf-8")).digest()
    return h[0] / 255.0


class _StubState:
    def __init__(self):
        self.requests: list[dict] = []
        self.mode = "normal"  # "normal" | "redirect"
        self.models = [probe_passage.EMBED_MODEL_ID, probe_passage.RERANK_MODEL_ID]

    def record(self, method, path, payload=None):
        self.requests.append({"method": method, "path": path, "payload": payload})


class _Handler(http_server_mod.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003 —— 静音默认访问日志
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self):
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        state = self.server.state
        state.record("GET", self.path)
        if state.mode == "redirect":
            self._redirect()
            return
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
        if state.mode == "redirect":
            self._redirect()
            return
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
        self.server = http_server_mod.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.state = _StubState()
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
# docker stub(monkeypatch scorer._run_docker——简报允许的两种方式之一)
# ======================================================================

def _default_exec_output(tag: str) -> str:
    return (
        f"{'a' * 63}{tag[-1]}  /app/.cache/huggingface/hub/models--BAAI--bge-m3/blobs/config-{tag}\n"
        f"{'b' * 63}{tag[-1]}  /app/.cache/huggingface/hub/models--BAAI--bge-m3/blobs/weight-{tag}\n"
    )


class FakeDocker:
    def __init__(self):
        self.container_image = "sha256:" + "1" * 64
        self.config_image = "sha256:" + "2" * 64  # 已是 digest 形式(unit 按 digest 启动的常态)
        self.started_at = "2026-07-17T00:00:00.000000000Z"
        self.repo_digest = "example.invalid/cc-infinity@sha256:" + "3" * 64
        self.exec_output = _default_exec_output("v1")
        self.calls: list[list[str]] = []

    def run(self, args, timeout=30.0):
        self.calls.append(list(args))
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

    def bump_restart(self, tag: str) -> None:
        """模拟容器重启:StartedAt 变化(marker 漂移)+ 权重指纹随之变化。"""
        self.started_at = f"2026-07-17T00:0{ord(tag[-1]) % 10}:00.000000000Z"
        self.exec_output = _default_exec_output(tag)


@pytest.fixture
def fake_docker(monkeypatch):
    fd = FakeDocker()
    monkeypatch.setattr(scorer, "_run_docker", fd.run)
    return fd


# ======================================================================
# Config / Ledger / BlobStore 构造 helper
# ======================================================================

def _build_env(tmp_path, infinity_base, container="test-infinity"):
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(exist_ok=True)
    ledger_dir.chmod(0o700)  # Ledger 构造要求 root 目录精确 0700(umask 可能冲掉 mkdir 的 mode)
    instance_dir = tmp_path / "instance"
    (instance_dir / ".cases").mkdir(parents=True, exist_ok=True)
    (instance_dir / "skills" / "demo-skill").mkdir(parents=True, exist_ok=True)
    (instance_dir / "skills" / "demo-skill" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    case_file = instance_dir / ".cases" / "agent_case-2026-07-17.md"
    case_file.write_text("---\nentry_count: 3\n---\n# cases\n", encoding="utf-8")
    pin_file = tmp_path / "PIN"
    pin_file.write_text("git_sha=deadbeef\nfreeze_hash=cafef00d\n", encoding="utf-8")
    env = {
        "EVEROS_MCP_PORT": "1",
        "EVEROS_MCP_TOKEN": "test-token",
        "EVEROS_BASE_URL": "http://127.0.0.1:1",
        "EVEROS_AGENT_ID": "test-agent",
        "INFINITY_BASE": infinity_base,
        "SHADOW_LEDGER_DIR": str(ledger_dir),
        "EVEROS_EMBED_MODEL": probe_passage.EMBED_MODEL_ID,
        "EVEROS_RERANK_MODEL": probe_passage.RERANK_MODEL_ID,
        "EVEROS_PIN_FILE": str(pin_file),
        "EVEROS_INSTANCE_DIR": str(instance_dir),
        "INFINITY_CONTAINER": container,
    }
    return env, ledger_dir


def _apply_env(monkeypatch, env):
    for k in list(os.environ):
        if k.startswith(("EVEROS_", "SHADOW_", "INFINITY_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _make_cfg(tmp_path, monkeypatch, infinity_base, container="test-infinity"):
    env, ledger_dir = _build_env(tmp_path, infinity_base, container)
    _apply_env(monkeypatch, env)
    return config_mod.load(), ledger_dir


# ======================================================================
# accepted 行 seeding helper(经真实 Ledger writer 提交,不是裸写 jsonl)
# ======================================================================

def _candidate(card_type, card_id, rank, passage_sha):
    return {
        "card_id": card_id,
        "card_type": card_type,
        "source_rank": rank,
        "native_score": 0.9,
        "payload_sha": f"payload-sha-{card_id}",
        "passage_sha": passage_sha,
        "truncated": False,
    }


def _seed_hit_query(led, store, rid, query, passages, ts=None):
    """`passages`: [(card_type, card_id, text), ...]。返回提交的 accepted 行。"""
    ts = ts if ts is not None else time.time()
    led.ops.submit(ledger_mod.ops_started(rid, "real"))
    led.ops.submit(ledger_mod.ops_terminal(rid, "hit"))
    candidates = []
    for rank, (card_type, card_id, text) in enumerate(passages):
        sha = store.put(text)
        candidates.append(_candidate(card_type, card_id, rank, sha))
    accepted = ledger_mod.accepted_row(
        "hit", rid, ts, "real", query=query, q_len=len(query),
        everos_rid="er-" + rid, candidates=candidates,
        returned_ids=[c["card_id"] for c in candidates],
        search_ms=5.0, pre_commit_ms=1.0, config_fp={"v": 1},
    )
    led.accepted.submit(accepted)
    return accepted


def _wait_for_scored(led, rid, timeout=10.0, predicate=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows, _ = ledger_mod.iter_rows(led.root, "scored")
        matches = [r for r in rows if r.get("rid") == rid and (predicate is None or predicate(r))]
        if matches:
            return matches
        time.sleep(0.05)
    raise AssertionError(f"等 rid={rid!r} 的 scored 行超时({timeout}s)")


# ======================================================================
# 1. 正常打分 → healthy ok 行 + 全 pins
# ======================================================================

def test_stub_normal_scoring_produces_healthy_ok_row_with_full_pins(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, ledger_dir = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    led = ledger_mod.Ledger(ledger_dir, scored_validator=materialize.healthy)
    store = blobstore_mod.BlobStore(ledger_dir)
    try:
        accepted = _seed_hit_query(
            led, store, "r1", "任务:调研 X",
            [("case", "c1", "案例卡内容"), ("skill", "s1", "技能卡内容")],
        )
        worker = scorer.ScoreWorker(cfg, led, store, queue_max=8, retry_backoff_base=0.01)
        try:
            assert worker.enqueue("r1") is True
            rows = _wait_for_scored(led, "r1")
        finally:
            worker.close(drain=True, timeout=5)

        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "ok"
        assert row["producer"] == "realtime"
        assert row["attempt_no"] == 0
        assert materialize.healthy(row, accepted) is True
        assert set(row["per_card"].keys()) == {"case:c1", "skill:s1"}
        assert materialize.PIN_KEYS.issubset(row["pins"].keys())
        for k in materialize.PIN_KEYS:
            assert row["pins"][k] not in (None, "unknown")
        assert row["lib_counts"] == {"case_count": 3, "skill_count": 1}
        assert isinstance(row["count_ts"], float)
    finally:
        led.close(drain=False)


# ======================================================================
# 2. 后端停摆 → 重试后 retryable_error;enqueue 响应路径不受影响
# ======================================================================

def test_backend_down_retries_then_retryable_error_enqueue_unaffected(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, ledger_dir = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    led = ledger_mod.Ledger(ledger_dir, scored_validator=materialize.healthy)
    store = blobstore_mod.BlobStore(ledger_dir)
    try:
        accepted = _seed_hit_query(led, store, "r-down", "任务 down", [("case", "c1", "内容 A")])
        worker = scorer.ScoreWorker(cfg, led, store, queue_max=8, retry_backoff_base=0.01)
        try:
            infinity_stub.shutdown()  # 打分后端此刻才停摆(pin 采集已在构造时完成)
            enqueued = worker.enqueue("r-down")
            assert enqueued is True  # 响应路径(enqueue)不受后端停摆影响
            rows = _wait_for_scored(led, "r-down", timeout=15.0)
        finally:
            worker.close(drain=True, timeout=5)

        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "retryable_error"
        assert row["score_error_code"] == "scoring_failed"
        assert materialize.healthy(row, accepted) is False
    finally:
        led.close(drain=False)


# ======================================================================
# 3. 队列满丢任务,accepted 仍在(不受影响)
# ======================================================================

def test_queue_full_drop_returns_false(tmp_path, monkeypatch, infinity_stub, fake_docker):
    cfg, ledger_dir = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    led = ledger_mod.Ledger(ledger_dir, scored_validator=materialize.healthy)
    store = blobstore_mod.BlobStore(ledger_dir)
    try:
        worker = scorer.ScoreWorker(cfg, led, store, queue_max=1, retry_backoff_base=0.01)
        started = threading.Event()
        release = threading.Event()

        def blocking_score_once(rid, producer):
            started.set()
            release.wait(timeout=5)

        worker._score_once = blocking_score_once  # 只在本测试内替换实例方法

        try:
            assert worker.enqueue("r-a") is True
            assert started.wait(timeout=5), "worker 未在预期时间内开始处理 r-a"
            # 此刻队列已空(r-a 已出队、正阻塞在处理中);queue_max=1 允许一个新任务排队
            assert worker.enqueue("r-b") is True
            # 第三个任务应该因为队列已满被丢弃
            assert worker.enqueue("r-c") is False
        finally:
            release.set()
            worker.close(drain=True, timeout=5)
    finally:
        led.close(drain=False)


# ======================================================================
# producer 冻结枚举(P2 回归测试:reconcile() 曾手误写 "reconcile" 而不是
# "reconciliation")
# ======================================================================

def test_producer_enum_pinned_to_three_values():
    assert scorer.PRODUCERS == frozenset({"realtime", "reconciliation", "manual"})


# ======================================================================
# 4. reconcile 补齐孤儿 + 失败 5 次落 permanent_failure 后不再扫
# ======================================================================

def test_reconcile_fills_orphan_and_permanent_failure_stops_rescan(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, ledger_dir = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    led = ledger_mod.Ledger(ledger_dir, scored_validator=materialize.healthy)
    store = blobstore_mod.BlobStore(ledger_dir)
    try:
        accepted_orphan = _seed_hit_query(
            led, store, "r-orphan", "孤儿查询", [("case", "c1", "孤儿内容")]
        )
        accepted_failed = _seed_hit_query(
            led, store, "r-failed", "失败查询", [("case", "c2", "失败内容")]
        )
        for _ in range(5):
            row = ledger_mod.scored_row(
                "r-failed", "realtime", "retryable_error", per_card={}, pins={},
                score_error_code="scoring_failed",
            )
            led.submit_scored(row, accepted_failed)

        worker = scorer.ScoreWorker(cfg, led, store, queue_max=8, retry_backoff_base=0.01)
        try:
            # P1d:reconcile() 不再直接调用打分 primitive——它只把待补打的 rid
            # 投递进 worker 的共享队列(`enqueue_reconcile`),真正的打分计算
            # 仍然只发生在 worker 唯一的消费线程里(互斥/并发 1 由此保证)。
            report = scorer.reconcile(
                cfg, led, worker.enqueue_reconcile, interval_between=0.0, fail_threshold=5
            )
        finally:
            # close(drain=True) 会先处理完已入队的 r-orphan 打分任务,再处理
            # sentinel——由此保证下面的断言能看到实际打分结果。
            worker.close(drain=True, timeout=5)

        assert report.orphans_found == 1
        assert report.permanent_failures == 1

        orphan_rows, _ = ledger_mod.iter_rows(ledger_dir, "scored")
        orphan_rows_r = [r for r in orphan_rows if r["rid"] == "r-orphan"]
        assert len(orphan_rows_r) == 1
        assert materialize.healthy(orphan_rows_r[0], accepted_orphan) is True

        failed_rows = [r for r in orphan_rows if r["rid"] == "r-failed"]
        assert len(failed_rows) == 6  # 原 5 条 retryable_error + 1 条新 permanent_failure
        assert failed_rows[-1]["status"] == "permanent_failure"
        # P2:`reconcile()` 落的 permanent_failure 行 producer 曾手误写成
        # "reconcile"(非枚举值),必须是冻结枚举里的 "reconciliation"。
        assert failed_rows[-1]["producer"] == "reconciliation"
        assert failed_rows[-1]["producer"] in scorer.PRODUCERS

        # 再扫一遍:r-failed 不应再被计入待补打集合(已终态失败)
        def _fail_if_called(rid):
            raise AssertionError(f"r-failed 不应被再次 rescore(rid={rid!r})")

        report2 = scorer.reconcile(
            cfg, led,
            lambda rid: _fail_if_called(rid) if rid == "r-failed" else True,
            interval_between=0.0, fail_threshold=5,
        )
        assert report2.permanent_failures == 0
        failed_rows_again, _ = ledger_mod.iter_rows(ledger_dir, "scored")
        assert len([r for r in failed_rows_again if r["rid"] == "r-failed"]) == 6
    finally:
        led.close(drain=False)


# ======================================================================
# 4b (P1d). reconciliation 与实时打分互斥(并发 1)——单一 worker 队列/线程是
# 唯一的打分执行路径,同一时刻最多一个 _score_once 在跑。
# ======================================================================

def test_reconcile_enqueue_while_worker_busy_never_scores_concurrently(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, ledger_dir = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    led = ledger_mod.Ledger(ledger_dir, scored_validator=materialize.healthy)
    store = blobstore_mod.BlobStore(ledger_dir)
    try:
        rids = [f"r-conc-{i}" for i in range(4)]
        accepted_by_rid = {
            rid: _seed_hit_query(led, store, rid, f"任务 {rid}", [("case", f"c-{rid}", f"内容 {rid}")])
            for rid in rids
        }

        worker = scorer.ScoreWorker(cfg, led, store, queue_max=8, retry_backoff_base=0.01)
        lock = threading.Lock()
        state = {"active": 0, "max_active": 0}
        real_score_once = worker._score_once

        def _tracked_score_once(rid, producer):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                time.sleep(0.05)  # 放大并发窗口,让潜在的竞态有机会暴露
                real_score_once(rid, producer)
            finally:
                with lock:
                    state["active"] -= 1

        worker._score_once = _tracked_score_once

        try:
            # 混合投递:realtime enqueue 与 reconcile 的 enqueue_reconcile 并发调用
            # (模拟 `_reconcile_loop` 周期扫描与真实查询响应路径同时发生)——
            # 两者共用同一条队列/同一个消费线程,并发只能是 1。
            def _reconcile_enqueue_all():
                for rid in rids[2:]:
                    worker.enqueue_reconcile(rid)

            t = threading.Thread(target=_reconcile_enqueue_all)
            for rid in rids[:2]:
                worker.enqueue(rid)
            t.start()
            t.join(timeout=5)

            for rid in rids:
                _wait_for_scored(led, rid, timeout=10.0)
        finally:
            worker.close(drain=True, timeout=5)

        assert state["max_active"] == 1
        for rid in rids:
            rows, _ = ledger_mod.iter_rows(ledger_dir, "scored")
            matches = [r for r in rows if r["rid"] == rid]
            assert len(matches) == 1
            assert materialize.healthy(matches[0], accepted_by_rid[rid]) is True
    finally:
        led.close(drain=False)


# ======================================================================
# 5. 缓存命中不重复调 embed(计数断言)
# ======================================================================

def test_cache_hit_does_not_reembed_passage(tmp_path, monkeypatch, infinity_stub, fake_docker):
    cfg, ledger_dir = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    led = ledger_mod.Ledger(ledger_dir, scored_validator=materialize.healthy)
    store = blobstore_mod.BlobStore(ledger_dir)
    try:
        _seed_hit_query(led, store, "r-first", "query one", [("case", "shared", "共享卡内容")])
        _seed_hit_query(led, store, "r-second", "query two", [("case", "shared", "共享卡内容")])

        worker = scorer.ScoreWorker(cfg, led, store, queue_max=8, retry_backoff_base=0.01)
        try:
            baseline = len(infinity_stub.state.requests)  # 跳过构造期 pin 采集的 embed 探测

            worker.manual_rescore("r-first")
            first_embed_reqs = [
                r for r in infinity_stub.state.requests[baseline:] if r["path"] == "/embeddings"
            ]
            assert len(first_embed_reqs) == 1
            assert set(first_embed_reqs[0]["payload"]["input"]) == {"query one", "共享卡内容"}

            worker.manual_rescore("r-second")
            all_embed_reqs = [
                r for r in infinity_stub.state.requests[baseline:] if r["path"] == "/embeddings"
            ]
            assert len(all_embed_reqs) == 2
            second_batch = all_embed_reqs[1]["payload"]["input"]
            assert second_batch == ["query two"]  # 共享卡的向量来自缓存,不重新 embed
        finally:
            worker.close(drain=True, timeout=5)
    finally:
        led.close(drain=False)


# ======================================================================
# 6. artifact_fp 变化(容器重启)→ 缓存失效,marker 漂移触发原子重建 + 重试
# ======================================================================

def test_artifact_fp_change_invalidates_cache_after_container_restart(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, ledger_dir = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    led = ledger_mod.Ledger(ledger_dir, scored_validator=materialize.healthy)
    store = blobstore_mod.BlobStore(ledger_dir)
    try:
        _seed_hit_query(led, store, "r-before", "query before", [("case", "shared", "共享卡内容")])
        _seed_hit_query(led, store, "r-after", "query after", [("case", "shared", "共享卡内容")])

        worker = scorer.ScoreWorker(cfg, led, store, queue_max=8, retry_backoff_base=0.01)
        try:
            worker.manual_rescore("r-before")
            fp_before = worker._pins_snapshot()["model_artifact_fp"]
            embed_reqs_before = len(
                [r for r in infinity_stub.state.requests if r["path"] == "/embeddings"]
            )

            fake_docker.bump_restart("v9")  # 模拟容器重启:StartedAt + 权重指纹都变了

            worker.manual_rescore("r-after")
            fp_after = worker._pins_snapshot()["model_artifact_fp"]
            assert fp_after != fp_before

            embed_reqs_after = [
                r for r in infinity_stub.state.requests if r["path"] == "/embeddings"
            ]
            # 重启后共享卡必须重新 embed(旧 artifact_fp 下的缓存不可复用)——
            # 断言"共享卡内容"字符串在重启之后至少又被作为 embed 输入出现过一次。
            reembedded = any(
                "共享卡内容" in req["payload"]["input"]
                for req in embed_reqs_after[embed_reqs_before:]
            )
            assert reembedded

            rows, _ = ledger_mod.iter_rows(ledger_dir, "scored")
            after_rows = [r for r in rows if r["rid"] == "r-after" and r["status"] == "ok"]
            assert len(after_rows) == 1
            assert after_rows[0]["pins"]["model_artifact_fp"] == fp_after
        finally:
            worker.close(drain=True, timeout=5)
    finally:
        led.close(drain=False)


# ======================================================================
# 7. 伪造 stub 返回 30x → raise 且零出站(第二请求永不发出)
# ======================================================================

def test_measure_embedding_dim_raises_on_redirect_with_zero_second_request(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, _ = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    infinity_stub.state.mode = "redirect"
    with pytest.raises(Exception):
        scorer.measure_embedding_dim(cfg)
    assert len(infinity_stub.state.requests) == 1  # 拒绝跟随重定向,第二个请求永不发出


# ======================================================================
# 7b (P1c). /models 探针也必须走出站唯一通道:redirect stub -> RedirectRefused,
# 零第二请求,经注入的 http.get_json 路径(scorer/server 实际接线的同一条路)。
# ======================================================================

def test_run_window_probe_models_redirect_refused_via_injected_get_json(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    from everos_mcp import http as http_mod

    cfg, _ = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    infinity_stub.state.mode = "redirect"
    with pytest.raises(http_mod.RedirectRefused):
        probe_passage.run_window_probe(cfg.infinity_base, get_json=http_mod.get_json)
    # /models 是 run_window_probe 的第一次出站——redirect 在 opener 层被拒绝,
    # 重定向目标请求永不发出,故只应记录到这一次请求。
    assert len(infinity_stub.state.requests) == 1


# ======================================================================
# 8. attempt_no 跨重启单调(重建 worker 读现有行)
# ======================================================================

def test_attempt_no_monotonic_across_worker_and_ledger_restart(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, ledger_dir = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)

    led1 = ledger_mod.Ledger(ledger_dir, scored_validator=materialize.healthy)
    store1 = blobstore_mod.BlobStore(ledger_dir)
    _seed_hit_query(led1, store1, "r-restart", "重启测试", [("case", "c1", "内容")])
    worker1 = scorer.ScoreWorker(cfg, led1, store1, queue_max=8, retry_backoff_base=0.01)
    worker1.manual_rescore("r-restart")
    worker1.close(drain=True, timeout=5)
    led1.close(drain=True)

    led2 = ledger_mod.Ledger(ledger_dir, scored_validator=materialize.healthy)
    store2 = blobstore_mod.BlobStore(ledger_dir)
    worker2 = scorer.ScoreWorker(cfg, led2, store2, queue_max=8, retry_backoff_base=0.01)
    try:
        worker2.manual_rescore("r-restart")
    finally:
        worker2.close(drain=True, timeout=5)
        led2.close(drain=False)

    rows, _ = ledger_mod.iter_rows(ledger_dir, "scored")
    rows_r = sorted(
        (r for r in rows if r["rid"] == "r-restart"), key=lambda r: r["attempt_no"]
    )
    assert [r["attempt_no"] for r in rows_r] == [0, 1]


# ======================================================================
# collect_pins() / collect_config_fp() / collect_lib_counts()——直接单测
# ======================================================================

def test_collect_pins_returns_exact_pin_keys_with_known_values(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, _ = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    pins = scorer.collect_pins(cfg)
    assert set(pins.keys()) == materialize.PIN_KEYS
    for k in materialize.PIN_KEYS:
        assert pins[k] not in (None, "unknown")
    assert pins["embed_model"] == probe_passage.EMBED_MODEL_ID
    assert pins["rerank_model"] == probe_passage.RERANK_MODEL_ID
    assert isinstance(pins["embedding_dim"], int) and pins["embedding_dim"] > 0


def test_collect_pins_raises_when_docker_inspect_fails(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, _ = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)

    def broken_run(args, timeout=30.0):
        return _CP(1, "", "no such container")

    monkeypatch.setattr(scorer, "_run_docker", broken_run)
    with pytest.raises(scorer.PinCollectionError):
        scorer.collect_pins(cfg)


def test_collect_pins_raises_when_marker_drifts_during_collection(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, _ = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    real_run = fake_docker.run
    calls = {"n": 0}

    def flaky_run(args, timeout=30.0):
        if args[0] == "inspect" and args[3] == "{{.State.StartedAt}}":
            calls["n"] += 1
            if calls["n"] == 2:  # 采集期间第二次读 marker 时"恰好"容器重启了
                return _CP(0, "2099-01-01T00:00:00.000000000Z", "")
        return real_run(args, timeout=timeout)

    monkeypatch.setattr(scorer, "_run_docker", flaky_run)
    with pytest.raises(scorer.PinCollectionError):
        scorer.collect_pins(cfg)


# ======================================================================
# PinFileCache(P2/R4 阻断项 #4):everos_pin 必须逐请求重读,不是 boot-cache
# ======================================================================

def test_pin_file_cache_rereads_after_mtime_change(tmp_path):
    pin_path = tmp_path / "PIN"
    pin_path.write_text("git_sha=aaa\n", encoding="utf-8")
    cache = scorer.PinFileCache(pin_path)
    assert cache.read() == "git_sha=aaa\n"

    # mtime 精度在某些文件系统上是秒级——显式往后拨一点,确保 mtime_ns 真的变化
    # (与内容大小同时变化时,mtime/size 任一变化都应触发重读;这里两者都变了)。
    new_mtime = pin_path.stat().st_mtime + 5
    pin_path.write_text("git_sha=bbb\n", encoding="utf-8")
    os.utime(pin_path, (new_mtime, new_mtime))
    assert cache.read() == "git_sha=bbb\n"


def test_pin_file_cache_does_not_reread_when_mtime_and_size_unchanged(tmp_path, monkeypatch):
    pin_path = tmp_path / "PIN"
    pin_path.write_text("git_sha=aaa\n", encoding="utf-8")
    cache = scorer.PinFileCache(pin_path)
    assert cache.read() == "git_sha=aaa\n"

    calls = {"n": 0}
    real_read_text = Path.read_text

    def _tracked_read_text(self, *a, **kw):
        calls["n"] += 1
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _tracked_read_text)
    assert cache.read() == "git_sha=aaa\n"  # 命中缓存,不应该真的再 read_text()
    assert calls["n"] == 0


def test_pin_file_cache_raises_when_file_missing_at_request_time(tmp_path):
    pin_path = tmp_path / "PIN"
    pin_path.write_text("git_sha=aaa\n", encoding="utf-8")
    cache = scorer.PinFileCache(pin_path)
    assert cache.read() == "git_sha=aaa\n"

    pin_path.unlink()
    with pytest.raises(scorer.PinCollectionError):
        cache.read()


def test_pin_file_cache_raises_pin_collection_error_when_unreadable(tmp_path):
    """P2:`read_text()` 抛出的异常(如 `PermissionError`)此前未被捕获,会
    原样冒出而不是统一映射成 `PinCollectionError`——只有 `stat()` 的失败被
    捕获。`chmod(0o000)` 后 `stat()` 仍能成功(只需目录可搜索),真正失败的
    是 `read_text()` 的 `open()` 调用,这正是本测试要钉住的绕过口。用一个
    从未读过、无缓存的全新 `PinFileCache` 实例,确保确实走到 `read_text()`
    而不是命中 mtime/size 缓存短路。"""
    pin_path = tmp_path / "PIN"
    pin_path.write_text("git_sha=aaa\n", encoding="utf-8")
    pin_path.chmod(0o000)
    try:
        cache = scorer.PinFileCache(pin_path)
        with pytest.raises(scorer.PinCollectionError):
            cache.read()
    finally:
        pin_path.chmod(0o600)


def test_collect_config_fp_uses_injected_pin_cache_and_rereads_on_swap(tmp_path, monkeypatch, infinity_stub, fake_docker):
    """collect_config_fp(cfg, pin_cache=...) 走注入的 PinFileCache——文件中途
    换内容,下一次调用必须拿到新值(不是 bootstrap 时算好就再也不变)。"""
    cfg, _ = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    cache = scorer.PinFileCache(cfg.pin_file)

    fp1 = scorer.collect_config_fp(cfg, pin_cache=cache)
    assert fp1["everos_pin"] == "git_sha=deadbeef\nfreeze_hash=cafef00d\n"

    new_mtime = cfg.pin_file.stat().st_mtime + 5
    cfg.pin_file.write_text("git_sha=newsha\nfreeze_hash=newfreeze\n", encoding="utf-8")
    os.utime(cfg.pin_file, (new_mtime, new_mtime))

    fp2 = scorer.collect_config_fp(cfg, pin_cache=cache)
    assert fp2["everos_pin"] == "git_sha=newsha\nfreeze_hash=newfreeze\n"
    # 静态字段不受影响
    assert fp2["agent_id"] == fp1["agent_id"] == "test-agent"


def test_collect_config_fp_reads_pin_file_raw_and_raises_if_missing(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, _ = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    fp = scorer.collect_config_fp(cfg)
    assert fp["agent_id"] == "test-agent"
    assert fp["top_k"] == 20
    assert fp["method"] == "hybrid"
    assert fp["payload_cap"] == 8000
    assert fp["everos_pin"] == "git_sha=deadbeef\nfreeze_hash=cafef00d\n"
    assert isinstance(fp["server_git_sha"], str) and len(fp["server_git_sha"]) == 40

    cfg.pin_file.unlink()
    with pytest.raises(scorer.PinCollectionError):
        scorer.collect_config_fp(cfg)


def test_collect_lib_counts_sums_entry_count_and_counts_skills(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, _ = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    # 追加第二个日聚合文件,验证"按文件求和"而不是"按文件数计"。
    (cfg.instance_dir / ".cases" / "agent_case-2026-07-18.md").write_text(
        "---\nentry_count: 7\n---\n# more\n", encoding="utf-8"
    )
    (cfg.instance_dir / "skills" / "second-skill").mkdir(parents=True)
    (cfg.instance_dir / "skills" / "second-skill" / "SKILL.md").write_text("# s2\n")

    lc = scorer.collect_lib_counts(cfg)
    assert lc["case_count"] == 3 + 7
    assert lc["skill_count"] == 2
    assert isinstance(lc["count_ts"], float)


def test_collect_lib_counts_raises_on_missing_cases_dir(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    cfg, _ = _make_cfg(tmp_path, monkeypatch, infinity_stub.base_url)
    import shutil

    shutil.rmtree(cfg.instance_dir / ".cases")
    with pytest.raises(scorer.LibCountsError):
        scorer.collect_lib_counts(cfg)


def test_get_image_digest_falls_back_to_repo_digests_when_not_digest_form(
    tmp_path, monkeypatch, infinity_stub, fake_docker
):
    fake_docker.config_image = "myrepo/cc-infinity:latest"  # 非 digest 形式
    digest = scorer.get_image_digest("test-infinity")
    assert digest == fake_docker.repo_digest
