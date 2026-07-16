"""集成 smoke(P1-8):fake Infinity HTTP stub,真 exec `scripts/probe_guard_mechanism.py`
到收尾。覆盖:缓存续跑(重跑同一 cache 文件网络调用显著下降)、meta 不符拒用
(`--code-git-sha` 变了 → `cache_rejected=True`)、error 终态(指向不存在的 Infinity
端点 → results.json 落 `status=error` + `failed_phase`,非零退出)、results.json
闭合(status=done 时 phase0..phase6 全部齐全)。

**本文件用 fake Infinity(GET /models、POST /embeddings、POST /rerank),不接触任何
真实分数**——task-6-brief 提到的"fake LiteLLM"在本 runner 里没有对应调用面(Task 6
的接口清单/probe_scores.py/probe_passage.py 全程只打 Infinity 的 /models、
/embeddings、/rerank,不经 LiteLLM),故只搭了 fake Infinity 这一个 stub。

fake Infinity 打分规则(内容驱动,不依赖候选在请求数组里的位置):文本里含
"★GOLD★" 标记,或(embeddings 场景下)查询文本以 "查询:" 开头 → 高分/同向量;
否则低分/异向量。cards.jsonl 里只有 `ac_gold`/`sk_gold` 两张卡带 ★GOLD★ 标记且
在全部 6 条查询下都是 gold-relevant∧useful,其余 31 张填充卡全部 gold-irrelevant
——这让判据引擎能在小样本上找到干净可行的 θ,不必依赖真实模型质量。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe_guard_mechanism.py"

sys.path.insert(0, str(SCRIPT.parent))
import probe_guard_mechanism as pgm  # noqa: E402(直接 import 供 pair 数断言负例等单元级测试用)

EMBED_MODEL_ID = "BAAI/bge-m3"
RERANK_MODEL_ID = "BAAI/bge-reranker-v2-m3"

GOLD_MARKER = "★GOLD★"
QUERY_PREFIX = "查询:"

CASE_IDS = ["ac_gold"] + [f"ac_f{i:02d}" for i in range(19)]  # 20
SKILL_IDS = ["sk_gold"] + [f"sk_f{i:02d}" for i in range(12)]  # 13
QUERY_IDS = [f"q{i}" for i in range(1, 7)]  # 6 条,3 组 × 2
GROUP_OF = {"q1": "sess-A", "q2": "sess-A", "q3": "sess-B", "q4": "sess-B",
            "q5": "sess-C", "q6": "sess-C"}


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _card_records() -> list[dict]:
    cards = [
        {"card_id": "ac_gold", "mem_type": "agent_case",
         "task_intent": f"{GOLD_MARKER} 合成金卡案例意图", "approach": f"{GOLD_MARKER} 合成金卡处理办法",
         "key_insight": f"{GOLD_MARKER} 合成金卡关键洞察", "title": "金卡", "text": "金卡正文(判卷用)"},
        {"card_id": "sk_gold", "mem_type": "agent_skill",
         "name": f"{GOLD_MARKER} 合成金卡技能", "description": f"{GOLD_MARKER} 合成金卡技能描述",
         "content": f"{GOLD_MARKER} 合成金卡技能正文", "title": "金卡技能", "text": "金卡技能正文(判卷用)"},
    ]
    for cid in CASE_IDS[1:]:
        cards.append({"card_id": cid, "mem_type": "agent_case",
                       "task_intent": f"普通案例意图 {cid}", "approach": f"普通案例办法 {cid}",
                       "key_insight": f"普通案例洞察 {cid}", "title": "填充", "text": f"填充正文 {cid}"})
    for cid in SKILL_IDS[1:]:
        cards.append({"card_id": cid, "mem_type": "agent_skill",
                       "name": f"普通技能 {cid}", "description": f"普通技能描述 {cid}",
                       "content": f"普通技能正文 {cid}", "title": "填充", "text": f"填充正文 {cid}"})
    return cards


def build_synthetic_probe_dataset(data_dir: Path, second_judge_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    second_judge_dir.mkdir(parents=True, exist_ok=True)

    cards = _card_records()
    _write_jsonl(data_dir / "cards.jsonl", cards)

    # raw_baseline:q2 与 q1 完全重复(raw eligibility 过滤的负例——重复组只保
    # query_id 最小者 q1,q2 应进 ineligible 列表),其余各自独立。
    def _raw(qid: str) -> str:
        return "合成用户消息 q1" if qid == "q2" else f"合成用户消息 {qid}"

    queryset = [
        {"query_id": qid, "external_id": f"{GROUP_OF[qid]}/subagents/agent-{qid}.jsonl",
         "source": "synthetic", "n_rounds": 5, "tier": "post_cutoff",
         "first_user_messages": [_raw(qid)], "raw_baseline": _raw(qid),
         "query": f"{QUERY_PREFIX}合成查询 {qid}"}
        for qid in QUERY_IDS
    ]
    _write_jsonl(data_dir / "queryset.jsonl", queryset)

    retrieval_records = []
    for qid in QUERY_IDS:
        agent_cases = [{"id": cid, "score": 0.95 - i * 0.02} for i, cid in enumerate(CASE_IDS)]
        agent_skills = [{"id": sid, "score": 0.95 - i * 0.02} for i, sid in enumerate(SKILL_IDS)]
        top5 = [
            {"id": "ac_gold", "mem_type": "agent_case"},
            {"id": "sk_gold", "mem_type": "agent_skill"},
            {"id": "ac_f00", "mem_type": "agent_case"},
            {"id": "ac_f01", "mem_type": "agent_case"},
            {"id": "sk_f00", "mem_type": "agent_skill"},
        ]
        retrieval_records.append({
            "query_id": qid, "variant": "synthetic", "top5": top5,
            "raw_response": {"agent_cases": agent_cases, "agent_skills": agent_skills},
        })
    _write_jsonl(data_dir / "retrieval.jsonl", retrieval_records)

    top5_job_records = []
    for row in retrieval_records:
        for rank, item in enumerate(row["top5"], 1):
            top5_job_records.append({
                "job_id": f"top5:{row['query_id']}:{rank}:{item['id']}", "kind": "top5",
                "query": f"top5-judge-text-{item['id']}", "rank": rank, "card_id": item["id"],
                "card_type": item["mem_type"], "card_text": "x",
            })
    _write_jsonl(data_dir / "top5_jobs.jsonl", top5_job_records)

    all_card_ids = [c["card_id"] for c in cards]
    card_type_by_id = {c["card_id"]: c["mem_type"] for c in cards}

    l1_records = []
    sj_job_records = []
    sj_verdict_records = []
    for qid in QUERY_IDS:
        for cid in all_card_ids:
            is_gold = cid in ("ac_gold", "sk_gold")
            l1_records.append({"job_id": f"l1:{qid}:{cid}", "relevant": is_gold, "useful": is_gold,
                               "reason": "synthetic"})
            sj_job_records.append({"job_id": f"sj:{qid}:{cid}", "kind": "sj",
                                   "query": f"sj-judge-text-{qid}", "card_id": cid,
                                   "card_type": card_type_by_id[cid], "card_text": "x"})
            sj_verdict_records.append({"job_id": f"sj:{qid}:{cid}", "relevant": is_gold,
                                       "useful": is_gold, "reason": "synthetic"})
    _write_jsonl(data_dir / "l1_verdicts.jsonl", l1_records)
    _write_jsonl(second_judge_dir / "jobs.jsonl", sj_job_records)
    _write_jsonl(second_judge_dir / "verdicts.jsonl", sj_verdict_records)


# ======================================================================
# fake Infinity HTTP stub(GET /models、POST /embeddings、POST /rerank)
# ======================================================================

class _Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.embeddings = 0
        self.rerank = 0

    def bump(self, name: str) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + 1)

    def snapshot(self) -> dict:
        with self.lock:
            return {"embeddings": self.embeddings, "rerank": self.rerank}


def _make_handler(counters: _Counters, fail_models: bool = False,
                   fail_rerank_docs_count: int | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静默,不刷测试输出
            pass

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/models":
                if fail_models:
                    self._send_json({"error": "synthetic failure"}, status=500)
                    return
                self._send_json({"data": [{"id": EMBED_MODEL_ID}, {"id": RERANK_MODEL_ID}]})
                return
            self._send_json({"error": "not found"}, status=404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")

            if self.path == "/embeddings":
                counters.bump("embeddings")
                texts = body["input"]
                data = []
                for i, t in enumerate(texts):
                    hot = t.startswith(QUERY_PREFIX) or GOLD_MARKER in t
                    vec = [1.0, 0.0, 0.0, 0.0] if hot else [0.0, 1.0, 0.0, 0.0]
                    data.append({"index": i, "embedding": vec})
                self._send_json({"data": data})
                return

            if self.path == "/rerank":
                counters.bump("rerank")
                docs = body["documents"]
                if fail_rerank_docs_count is not None and len(docs) == fail_rerank_docs_count:
                    # 定向失败:只有恰好这个 doc 数的 rerank 调用返回 500——用来模拟
                    # "某一臂的延迟调用图挂了,其余臂照常"(56 = null_ref 独有的 pair 数)。
                    self._send_json({"error": "synthetic rerank failure"}, status=500)
                    return
                results = []
                for i, d in enumerate(docs):
                    score = 0.9 if GOLD_MARKER in d else 0.1
                    results.append({"index": i, "relevance_score": score})
                self._send_json({"results": results})
                return

            self._send_json({"error": "not found"}, status=404)

    return Handler


class FakeInfinity:
    def __init__(self, fail_models: bool = False, fail_rerank_docs_count: int | None = None):
        self.counters = _Counters()
        handler = _make_handler(self.counters, fail_models=fail_models,
                                 fail_rerank_docs_count=fail_rerank_docs_count)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
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


@pytest.fixture
def fake_infinity():
    server = FakeInfinity().start()
    yield server
    server.stop()


def _run(tmp_path: Path, fake_infinity: FakeInfinity, *, out_name: str = "out",
         cache_name: str | None = "cache.json", code_git_sha: str | None = None,
         infinity_base: str | None = None, extra_args: list[str] | None = None
         ) -> subprocess.CompletedProcess:
    data_dir = tmp_path / "data"
    sj_dir = tmp_path / "second_judge"
    if not data_dir.exists():
        build_synthetic_probe_dataset(data_dir, sj_dir)

    out_dir = tmp_path / out_name
    args = [
        sys.executable, str(SCRIPT),
        "--data-dir", str(data_dir),
        "--second-judge-dir", str(sj_dir),
        "--infinity-base", infinity_base or fake_infinity.base_url,
        "--out-dir", str(out_dir),
        "--drift-rounds", "1",
        "--latency-queries", "2",
        "--latency-reps", "3",
        "--latency-warmup", "1",
        "--latency-timeout", "5",
        "--timeout", "10",
    ]
    if cache_name is not None:
        args += ["--cache-path", str(tmp_path / cache_name)]
    if code_git_sha is not None:
        args += ["--code-git-sha", code_git_sha]
    if extra_args:
        args += extra_args

    return subprocess.run(args, capture_output=True, text=True, timeout=120)


# ======================================================================
# results.json 闭合 + 全流程走通
# ======================================================================

def test_runs_end_to_end_and_results_json_is_closed(tmp_path, fake_infinity):
    result = _run(tmp_path, fake_infinity, code_git_sha="deadbeef01")
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    results_path = tmp_path / "out" / "results.json"
    assert results_path.exists()
    state = json.loads(results_path.read_text(encoding="utf-8"))
    assert state["status"] == "done"

    phases = state["phases"]
    for name in ("phase0_integrity", "phase1_known_control", "phase2_scoring",
                 "phase3_fit_and_cv", "phase4_sensitivity", "phase5_drift", "phase6_latency"):
        assert name in phases, f"missing phase {name}"

    per_arm = phases["phase3_fit_and_cv"]["per_arm"]
    assert set(per_arm) == {"native_pertype", "cos_unified", "cos_pertype",
                             "ce_fixed", "ce_znorm", "null_ref"}
    survivors = [name for name, entry in per_arm.items() if entry["survives"]]
    assert survivors, "fixture 设计为至少一臂应幸存(GOLD 标记卡分数干净分离),结果里一个都没有"

    # contamination floor 自动推导:fixture top5 = 2 gold + 3 filler → 每查询 FDR 3/5,
    # 全部 covered → 基线宏 FDR 0.6,floor = round(0.6/2, 2) = 0.3(审计字段齐全)
    floor_info = state["contamination_floor"]
    assert floor_info["source"] == "derived"
    assert floor_info["baseline_macro_fdr"] == pytest.approx(0.6)
    assert floor_info["floor"] == pytest.approx(0.3)
    assert "round(baseline / 2, 2)" in floor_info["formula"]

    # 幸存臂必须在 phase4/5/6 里各有一条诊断记录(cv/final/transport/latency 四套数据闭合)
    for arm_name in survivors:
        assert arm_name in phases["phase4_sensitivity"]["arms"]
        assert arm_name in phases["phase5_drift"]["arms"]
        assert arm_name in phases["phase6_latency"]["arms"]
        # 方向稳定性诊断(P1-12):三变体标签 + direction_stable 布尔齐全
        ds = phases["phase4_sensitivity"]["arms"][arm_name]["direction_stability"]
        assert set(ds["labels"]) == {"primary", "sens_rel", "sens_irr"}
        assert isinstance(ds["direction_stable"], bool)
        # 延迟门:fake Infinity 亚毫秒,应全 PASS
        assert phases["phase6_latency"]["arms"][arm_name]["latency_gate"] == "PASS"

    # raw variant 敏感性(纯诊断):q2 与 q1 的 raw_baseline 完全重复 → q2 ineligible
    raw = phases["phase4_sensitivity"]["raw_diagnostic"]
    assert [r["query_id"] for r in raw["ineligible"]] == ["q2"]
    assert raw["ineligible"][0]["kept"] == "q1"
    assert raw["n_eligible"] == 5
    assert raw["skipped"] == {}  # fake fixture 文本极短,不该有超 PAIR_BUDGET 的
    for arm_name in survivors:
        assert arm_name in raw["per_arm"]

    assert phases["phase2_scoring"]["cache_rejected"] is False  # 首次跑,无旧缓存可拒


# ======================================================================
# 缓存续跑:重跑同一 cache 文件,网络调用显著下降
# ======================================================================

def test_cache_resume_significantly_reduces_network_calls(tmp_path, fake_infinity):
    result1 = _run(tmp_path, fake_infinity, code_git_sha="deadbeef01")
    assert result1.returncode == 0, f"stdout={result1.stdout!r} stderr={result1.stderr!r}"
    calls_after_first = fake_infinity.counters.snapshot()
    total_first = calls_after_first["embeddings"] + calls_after_first["rerank"]

    result2 = _run(tmp_path, fake_infinity, out_name="out2", code_git_sha="deadbeef01")
    assert result2.returncode == 0, f"stdout={result2.stdout!r} stderr={result2.stderr!r}"
    calls_after_second = fake_infinity.counters.snapshot()
    total_second_run_only = (calls_after_second["embeddings"] + calls_after_second["rerank"]) - total_first

    assert total_second_run_only < total_first * 0.5, (
        f"缓存续跑应显著减少网络调用:第一轮 {total_first} 次,第二轮新增 {total_second_run_only} 次"
    )

    state2 = json.loads((tmp_path / "out2" / "results.json").read_text(encoding="utf-8"))
    assert state2["phases"]["phase2_scoring"]["cache_rejected"] is False  # meta 相同,复用缓存


# ======================================================================
# meta 不符拒用
# ======================================================================

def test_cache_rejected_wholesale_on_meta_mismatch(tmp_path, fake_infinity):
    result1 = _run(tmp_path, fake_infinity, code_git_sha="sha-one")
    assert result1.returncode == 0, f"stdout={result1.stdout!r} stderr={result1.stderr!r}"

    result2 = _run(tmp_path, fake_infinity, out_name="out2", code_git_sha="sha-two")
    assert result2.returncode == 0, f"stdout={result2.stdout!r} stderr={result2.stderr!r}"

    state2 = json.loads((tmp_path / "out2" / "results.json").read_text(encoding="utf-8"))
    assert state2["phases"]["phase2_scoring"]["cache_rejected"] is True


# ======================================================================
# error 终态
# ======================================================================

def test_error_terminal_state_on_unreachable_infinity(tmp_path):
    data_dir = tmp_path / "data"
    sj_dir = tmp_path / "second_judge"
    build_synthetic_probe_dataset(data_dir, sj_dir)

    out_dir = tmp_path / "out"
    args = [
        sys.executable, str(SCRIPT),
        "--data-dir", str(data_dir),
        "--second-judge-dir", str(sj_dir),
        "--infinity-base", "http://127.0.0.1:1",  # 端口 1:连接必被拒
        "--out-dir", str(out_dir),
        "--timeout", "3",
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)

    assert result.returncode != 0
    results_path = out_dir / "results.json"
    assert results_path.exists()
    state = json.loads(results_path.read_text(encoding="utf-8"))
    assert state["status"] == "error"
    assert state["failed_phase"] == "phase0_integrity"
    assert "error" in state and state["error"]


def test_error_terminal_state_when_models_endpoint_fails(tmp_path):
    server = FakeInfinity(fail_models=True).start()
    try:
        result = _run(tmp_path, server, code_git_sha="deadbeef01")
    finally:
        server.stop()

    assert result.returncode != 0
    state = json.loads((tmp_path / "out" / "results.json").read_text(encoding="utf-8"))
    assert state["status"] == "error"
    assert state["failed_phase"] == "phase0_integrity"


# ======================================================================
# 延迟门:一臂 error 只 FAIL 该臂,其余臂照常收尾;floor override 审计留痕
# ======================================================================

def test_latency_error_fails_only_that_arm_and_run_still_completes(tmp_path):
    # 56 = null_ref 独有的调用图 pair 数(40 候选 + 16 decoy);定向让恰 56 doc 的
    # rerank 返回 500 → 只有 null_ref 的延迟调用会炸(phase2/4 的 rerank batch 是
    # 33 候选/8 decoy/40 ce,都撞不上 56)。
    server = FakeInfinity(fail_rerank_docs_count=56).start()
    try:
        result = _run(tmp_path, server, code_git_sha="deadbeef01",
                      extra_args=["--contamination-floor-override", "0.5"])
    finally:
        server.stop()

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    state = json.loads((tmp_path / "out" / "results.json").read_text(encoding="utf-8"))
    assert state["status"] == "done"  # 一臂延迟 FAIL 不打断整跑

    # floor override 审计留痕(测试注入口,生产路径不用)
    assert state["contamination_floor"]["floor"] == pytest.approx(0.5)
    assert state["contamination_floor"]["source"].startswith("override")

    arms = state["phases"]["phase6_latency"]["arms"]
    assert arms["null_ref"]["latency_gate"] == "FAIL"
    assert arms["null_ref"]["error"]  # 原因必须记录
    others = [name for name in arms if name != "null_ref"]
    assert others, "除 null_ref 外应有其它幸存臂进入延迟账"
    for name in others:
        assert arms[name]["latency_gate"] == "PASS"


# ======================================================================
# pair 数断言(冻结 ce=40 / null_ref=56):调用图漂移必须 fail-loud
# ======================================================================

def _latency_args() -> argparse.Namespace:
    # 断言在发请求前触发,端口 1 永远不会被真正连上
    return argparse.Namespace(infinity_base="http://127.0.0.1:1", rerank_model="m",
                               embed_model="m", latency_timeout=1)


def test_latency_pair_count_assertion_fires_on_decoy_drift(monkeypatch):
    fixture = {"cases": ["c"] * 20, "skills": ["s"] * 20}
    # decoy 文件漂移:case 侧只剩 7 段 → null_ref 有效 pair 数 40+15=55 != 56
    monkeypatch.setattr(pgm, "_load_decoys",
                        lambda s: {"agent_case": ["d"] * 7, "agent_skill": ["d"] * 8})
    with pytest.raises(pgm.RunnerError, match="56"):
        pgm._latency_call("null_ref", "q", fixture, _latency_args())


def test_latency_pair_count_assertion_fires_on_fixture_drift_for_ce():
    fixture = {"cases": ["c"] * 19, "skills": ["s"] * 20}  # 39 != 冻结值 40
    with pytest.raises(pgm.RunnerError, match="40"):
        pgm._latency_call("ce_fixed", "q", fixture, _latency_args())
