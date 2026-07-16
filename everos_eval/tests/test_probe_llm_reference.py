"""P5 §Task 7:LLM 参照臂脚本测试(mock LiteLLM,零真实调用)。

覆盖:口径 A 忠实抽取 + sha 稳定、模型发现(零/多候选报错)、回执解析校验
(单行/类型/useful⇒relevant)、坏行重试与 error 终态、幂等续跑(job_id 跳过,
不重发请求)、判据引擎复用产出(complete 精确值 / incomplete 上下界)。

fixture 复用 `test_probe_guard_mechanism_smoke.build_synthetic_probe_dataset`
(同一套 6 查询 × 33 候选合成数据,`ac_gold`/`sk_gold` 两张卡是唯一 gold-relevant
∧useful 候选)——避免重复维护一份满足 `load_candidates`(恰 33 候选)与
`load_gold`(queryset/cards/retrieval/l1_verdicts/second_judge 全套闭合)硬断言
的合成数据集。fake LiteLLM 判定规则:卡文本含"金卡"(仅 ac_gold/sk_gold 的
`text` 字段命中)→ relevant=useful=true,否则 false——与该合成数据集的 gold
标签(仅这两张卡在全部查询下 gold-relevant∧useful)精确重合，能验证判据引擎
在"LLM 判定完全命中 gold"时给出干净可行的结果。
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import probe_llm_reference as plr  # noqa: E402

from everos_eval.probe_gold import load_gold  # noqa: E402
from everos_eval.tests.test_probe_guard_mechanism_smoke import (  # noqa: E402
    build_synthetic_probe_dataset,
)

MODEL_ID = "deepseek-v4-flash"


# ======================================================================
# fake LiteLLM HTTP stub(GET /models、POST /chat/completions)
# ======================================================================

class _Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.chat_calls = 0

    def bump(self) -> int:
        with self.lock:
            self.chat_calls += 1
            return self.chat_calls


def _default_verdict_for(body: dict) -> dict:
    user_content = body["messages"][-1]["content"]
    hit = "金卡" in user_content
    return {"relevant": hit, "useful": hit, "reason": "fake judge"}


def _make_handler(counters: _Counters, *, model_ids: list[str],
                   verdict_fn=_default_verdict_for, bad_for_substring: str | None = None,
                   bad_forever_for_substring: str | None = None, fail_models: bool = False):
    """`bad_for_substring`:命中该子串的调用第一次返回坏内容,之后(重试)正常
    ——用来测"坏行重试后成功"。`bad_forever_for_substring`:命中的调用永远返回
    坏内容——用来测"重试耗尽 → error 终态"。"""
    seen_bad_once: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
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
                self._send_json({"data": [{"id": mid} for mid in model_ids]})
                return
            self._send_json({"error": "not found"}, status=404)

        def do_POST(self):
            if self.path != "/chat/completions":
                self._send_json({"error": "not found"}, status=404)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            counters.bump()
            user_content = body["messages"][-1]["content"]

            content: str
            if bad_forever_for_substring is not None and bad_forever_for_substring in user_content:
                content = "不是 JSON,纯文字回复"
            elif (bad_for_substring is not None and bad_for_substring in user_content
                  and bad_for_substring not in seen_bad_once):
                seen_bad_once.add(bad_for_substring)
                content = "第一次坏掉的回执"
            else:
                content = json.dumps(verdict_fn(body), ensure_ascii=False)

            self._send_json({
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            })

    return Handler


class FakeLiteLLM:
    def __init__(self, *, model_ids: list[str] = (MODEL_ID,), **handler_kwargs):
        self.counters = _Counters()
        handler = _make_handler(self.counters, model_ids=list(model_ids), **handler_kwargs)
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
def fake_litellm():
    server = FakeLiteLLM().start()
    yield server
    server.stop()


FROZEN_MD = """# Judge Prompts — FROZEN

## 口径 A:相关性 + 实质帮助(l1 与 top5 共用)

给你一条「任务查询」和一张「记忆卡」。逐级判定:

1. relevant(相关)
2. useful(实质帮助,仅 relevant=true 时判)
3. 输出 JSON:{"job_id": "...", "relevant": bool, "useful": bool, "reason": "..."}

## 口径 B:foresight 三分类

给你一条 foresight。三分类:...

## 口径 C:查询生成

给你一个工作会话的开头消息。...
"""


# ======================================================================
# 口径 A 忠实抽取 + sha 稳定
# ======================================================================

def test_extract_criterion_a_stops_before_criterion_b():
    extracted = plr.extract_criterion_a(FROZEN_MD)
    assert extracted.startswith("## 口径 A:相关性 + 实质帮助(l1 与 top5 共用)")
    assert "口径 B" not in extracted
    assert "口径 C" not in extracted
    assert "job_id" in extracted  # 原文里的输出格式描述原样保留(语义零改动)


def test_extract_criterion_a_missing_section_raises():
    with pytest.raises(plr.RunnerError, match="口径 A"):
        plr.extract_criterion_a("# 没有口径 A 标题的文档\n\n随便什么内容\n")


def test_extract_criterion_a_sha_stable_across_calls():
    a1 = plr.extract_criterion_a(FROZEN_MD)
    a2 = plr.extract_criterion_a(FROZEN_MD)
    assert plr.sha256_text(a1) == plr.sha256_text(a2)
    # 内容变了 sha 必须跟着变(不是常量/占位符)
    tampered = FROZEN_MD.replace("relevant(相关)", "relevant(改过的)")
    a3 = plr.extract_criterion_a(tampered)
    assert plr.sha256_text(a1) != plr.sha256_text(a3)


def test_build_system_prompt_wrapper_not_included_in_sha():
    criterion_a = plr.extract_criterion_a(FROZEN_MD)
    sha_before = plr.sha256_text(criterion_a)
    system_prompt = plr.build_system_prompt(criterion_a)
    assert system_prompt.startswith(criterion_a)
    assert "只输出一行 JSON" in system_prompt  # 输出 schema 包装确实追加了
    # sha 只对抽取段计算,包装是运行时内存追加,不改变已记录的 sha
    assert plr.sha256_text(criterion_a) == sha_before


# ======================================================================
# 模型发现
# ======================================================================

def test_discover_model_unique_match(fake_litellm):
    model = plr.discover_model(fake_litellm.base_url, "k")
    assert model == MODEL_ID


def test_discover_model_zero_candidates_raises():
    server = FakeLiteLLM(model_ids=["gpt-4o", "claude-3"]).start()
    try:
        with pytest.raises(plr.RunnerError, match="候选数=0"):
            plr.discover_model(server.base_url, "k")
    finally:
        server.stop()


def test_discover_model_multiple_candidates_raises():
    server = FakeLiteLLM(model_ids=["deepseek-v4-flash", "deepseek-v4-flash-lite"]).start()
    try:
        with pytest.raises(plr.RunnerError, match="候选数=2"):
            plr.discover_model(server.base_url, "k")
    finally:
        server.stop()


def test_discover_model_endpoint_failure_raises():
    server = FakeLiteLLM(fail_models=True).start()
    try:
        with pytest.raises(plr.RunnerError, match="HTTP 500"):
            plr.discover_model(server.base_url, "k")
    finally:
        server.stop()


# ======================================================================
# 回执解析校验
# ======================================================================

def test_parse_verdict_valid():
    v = plr.parse_verdict('{"relevant": true, "useful": true, "reason": "命中"}')
    assert v == {"relevant": True, "useful": True, "reason": "命中"}


def test_parse_verdict_rejects_multiline():
    with pytest.raises(ValueError, match="非单行"):
        plr.parse_verdict('{"relevant": true,\n"useful": true, "reason": "x"}')


def test_parse_verdict_rejects_bad_json():
    with pytest.raises(ValueError, match="非合法 JSON"):
        plr.parse_verdict("这不是 JSON")


def test_parse_verdict_rejects_wrong_types():
    with pytest.raises(ValueError, match="字段类型错误"):
        plr.parse_verdict('{"relevant": "true", "useful": true, "reason": "x"}')


def test_parse_verdict_rejects_useful_without_relevant():
    with pytest.raises(ValueError, match="useful⇒relevant"):
        plr.parse_verdict('{"relevant": false, "useful": true, "reason": "x"}')


def test_parse_verdict_rejects_missing_reason():
    with pytest.raises(ValueError, match="reason"):
        plr.parse_verdict('{"relevant": true, "useful": false}')


# ======================================================================
# judge_one:重试后成功 / 重试耗尽 error 终态
# ======================================================================

def test_judge_one_succeeds_first_try(fake_litellm):
    result = plr.judge_one(fake_litellm.base_url, "k", MODEL_ID, "sys", "q", "含金卡的卡文本",
                            timeout=10)
    assert result["ok"] is True
    assert result["verdict"] == {"relevant": True, "useful": True, "reason": "fake judge"}
    assert result["attempts"] == 1
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_judge_one_retries_then_succeeds():
    server = FakeLiteLLM(bad_for_substring="重试专用标记").start()
    try:
        result = plr.judge_one(server.base_url, "k", MODEL_ID, "sys", "q",
                                "普通卡文本 重试专用标记", timeout=10)
        assert result["ok"] is True
        assert result["attempts"] == 2  # 第一次坏,第二次成功
        # 两次调用的 usage 都应计入(失败调用同样计费)
        assert result["usage"]["total_tokens"] == 30
    finally:
        server.stop()


def test_judge_one_exhausts_retries_records_error():
    server = FakeLiteLLM(bad_forever_for_substring="永远坏标记").start()
    try:
        result = plr.judge_one(server.base_url, "k", MODEL_ID, "sys", "q", "永远坏标记",
                                timeout=10, max_retries=2)
        assert result["ok"] is False
        assert result["attempts"] == 3  # 1 + 2 次重试
        assert "非单行" in result["error"] or "非合法 JSON" in result["error"]
        assert result["usage"]["total_tokens"] == 45  # 3 次调用都计费
    finally:
        server.stop()


# ======================================================================
# load_jobs:990 对(本 fixture 6 查询 × 33 候选 = 198)
# ======================================================================

def test_load_jobs_produces_query_times_candidates_pairs(tmp_path):
    data_dir = tmp_path / "data"
    sj_dir = tmp_path / "second_judge"
    build_synthetic_probe_dataset(data_dir, sj_dir)

    jobs = plr.load_jobs(data_dir)
    assert len(jobs) == 6 * 33
    job_ids = {j["job_id"] for j in jobs}
    assert len(job_ids) == len(jobs)  # 唯一
    assert all(j["job_id"].startswith("llm:") for j in jobs)

    gold_job = next(j for j in jobs if j["canonical_card_id"] == "ac_gold" and j["query_id"] == "q1")
    assert gold_job["card_text"] == "金卡正文(判卷用)"
    assert gold_job["mem_type"] == "agent_case"


# ======================================================================
# run_jobs:幂等续跑(job_id 跳过,不重发请求)
# ======================================================================

def test_run_jobs_idempotent_resume_skips_done_jobs(tmp_path, fake_litellm):
    data_dir = tmp_path / "data"
    sj_dir = tmp_path / "second_judge"
    build_synthetic_probe_dataset(data_dir, sj_dir)
    jobs = plr.load_jobs(data_dir)
    ledger_path = tmp_path / "out" / "llm_verdicts.jsonl"

    ledger1 = plr.run_jobs(jobs, ledger_path, base=fake_litellm.base_url, key="k", model=MODEL_ID,
                            system_prompt="sys", timeout=10)
    assert len(ledger1) == len(jobs)
    calls_after_first = fake_litellm.counters.chat_calls
    assert calls_after_first == len(jobs)

    # 续跑:同一个 ledger 文件,job_id 全部命中,零新请求
    ledger2 = plr.run_jobs(jobs, ledger_path, base=fake_litellm.base_url, key="k", model=MODEL_ID,
                            system_prompt="sys", timeout=10)
    assert len(ledger2) == len(jobs)
    assert fake_litellm.counters.chat_calls == calls_after_first  # 没有新调用


def test_run_jobs_resume_also_skips_terminal_error_jobs(tmp_path):
    data_dir = tmp_path / "data"
    sj_dir = tmp_path / "second_judge"
    build_synthetic_probe_dataset(data_dir, sj_dir)
    jobs = plr.load_jobs(data_dir)
    ledger_path = tmp_path / "out" / "llm_verdicts.jsonl"

    server = FakeLiteLLM(bad_forever_for_substring="填充正文 ac_f00").start()
    try:
        ledger1 = plr.run_jobs(jobs, ledger_path, base=server.base_url, key="k", model=MODEL_ID,
                                system_prompt="sys", timeout=10, )
        errored = [j for j, r in ledger1.items() if not r["ok"]]
        assert errored  # ac_f00 那几条(每查询一条)应终态失败
        calls_after_first = server.counters.chat_calls

        ledger2 = plr.run_jobs(jobs, ledger_path, base=server.base_url, key="k", model=MODEL_ID,
                                system_prompt="sys", timeout=10)
        assert server.counters.chat_calls == calls_after_first  # error 终态同样跳过,不重试
        assert ledger2 == ledger1
    finally:
        server.stop()


# ======================================================================
# build_output:complete 精确值 / incomplete 上下界(判据引擎复用)
# ======================================================================

def _setup(tmp_path):
    data_dir = tmp_path / "data"
    sj_dir = tmp_path / "second_judge"
    build_synthetic_probe_dataset(data_dir, sj_dir)
    jobs = plr.load_jobs(data_dir)
    gold = load_gold(data_dir, sj_dir)
    sq_by_qid = plr._scored_queries(jobs)
    return jobs, gold, sq_by_qid, data_dir, sj_dir


def test_build_output_complete_when_all_judged_ok(tmp_path, fake_litellm):
    jobs, gold, sq_by_qid, data_dir, sj_dir = _setup(tmp_path)
    ledger_path = tmp_path / "out" / "llm_verdicts.jsonl"
    ledger = plr.run_jobs(jobs, ledger_path, base=fake_litellm.base_url, key="k", model=MODEL_ID,
                           system_prompt="sys", timeout=10)

    out = plr.build_output(jobs, ledger, sq_by_qid, gold["primary"], model=MODEL_ID,
                            prompt_sha="deadbeef")
    assert out["completeness"] == "complete"
    assert out["judged_ok"] == len(jobs) == 198
    assert out["errors"] == []
    assert out["production_candidate"] is False
    assert out["contamination_floor"] == plr.CONTAMINATION_FLOOR
    # LLM 判定与 gold 完全重合(只放行 ac_gold/sk_gold)→ 应该干净可行
    assert out["passed"] is True
    assert out["floors"]["abstain_rate"] == 1.0  # 该 fixture 全查询 covered,uncovered 空集恒 1.0
    assert out["floors"]["useful_rate"] == 1.0
    assert out["floors"]["macro_fdr"] == 0.0
    for qid, returned in out["returned_by_qid"].items():
        assert returned == sorted({"ac_gold", "sk_gold"})
    assert out["verdict_stats"]["relevant_count"] == 6 * 2  # 每查询 2 张 gold 卡
    assert out["token_usage"]["total_tokens"] == 198 * 15


def test_build_output_incomplete_reports_both_boundary_scenarios(tmp_path):
    jobs, gold, sq_by_qid, data_dir, sj_dir = _setup(tmp_path)
    ledger_path = tmp_path / "out" / "llm_verdicts.jsonl"

    # sk_gold 在全部查询上永远判失败(终态 error)——其余候选正常判
    server = FakeLiteLLM(bad_forever_for_substring="金卡技能正文(判卷用)").start()
    try:
        ledger = plr.run_jobs(jobs, ledger_path, base=server.base_url, key="k", model=MODEL_ID,
                               system_prompt="sys", timeout=10)
    finally:
        server.stop()

    out = plr.build_output(jobs, ledger, sq_by_qid, gold["primary"], model=MODEL_ID,
                            prompt_sha="deadbeef")
    assert out["completeness"] == "incomplete"
    assert len(out["errors"]) == 6  # 每查询一条 sk_gold error
    assert all(e["job_id"].endswith(":sk_gold") for e in out["errors"])
    assert "floors" not in out  # 精确值不该在 incomplete 时出现
    assert set(out["bounds"]) == {"missing_treated_as_allow", "missing_treated_as_block"}

    allow_returned = out["bounds"]["missing_treated_as_allow"]["returned_by_qid"]["q1"]
    block_returned = out["bounds"]["missing_treated_as_block"]["returned_by_qid"]["q1"]
    assert "sk_gold" in allow_returned  # 全放行边界:缺失的 sk_gold 判定按放行处理
    assert "sk_gold" not in block_returned  # 全拦截边界:缺失的候选不放行
    assert "ac_gold" in allow_returned and "ac_gold" in block_returned  # 已判定 ok 的不受边界影响

    # 全放行边界比全拦截边界放进更多卡 → 全拦截边界的 useful_rate 应 <= 全放行边界
    allow_floors = out["bounds"]["missing_treated_as_allow"]["floors"]
    block_floors = out["bounds"]["missing_treated_as_block"]["floors"]
    assert block_floors["useful_rate"] <= allow_floors["useful_rate"]


# ======================================================================
# main():端到端 CLI(fake LiteLLM,产出文件校验)
# ======================================================================

def test_main_end_to_end_writes_output_files(tmp_path, fake_litellm, monkeypatch):
    data_dir = tmp_path / "data"
    sj_dir = tmp_path / "second_judge"
    build_synthetic_probe_dataset(data_dir, sj_dir)
    prompt_path = tmp_path / "judge-prompts-frozen.md"
    prompt_path.write_text(FROZEN_MD, encoding="utf-8")
    out_dir = tmp_path / "out"

    monkeypatch.delenv("PROBE_PROMPT_PATH", raising=False)
    monkeypatch.delenv("LITELLM_LLM_BASE", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_ADMIN_KEY", raising=False)

    rc = plr.main([
        "--data-dir", str(data_dir),
        "--second-judge-dir", str(sj_dir),
        "--out-dir", str(out_dir),
        "--prompt-path", str(prompt_path),
        "--litellm-base", fake_litellm.base_url,
        "--litellm-key", "test-key",
        "--timeout", "10",
    ])
    assert rc == 0

    result_path = out_dir / "llm_reference.json"
    ledger_path = out_dir / "llm_verdicts.jsonl"
    assert result_path.exists()
    assert ledger_path.exists()

    output = json.loads(result_path.read_text(encoding="utf-8"))
    assert output["model"] == MODEL_ID
    assert output["completeness"] == "complete"
    assert output["production_candidate"] is False
    assert output["prompt_criterion_a_sha256"] == plr.sha256_text(
        plr.extract_criterion_a(FROZEN_MD)
    )

    ledger_lines = [json.loads(l) for l in ledger_path.read_text(encoding="utf-8").splitlines()]
    assert len(ledger_lines) == 198


def test_main_missing_required_env_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("PROBE_PROMPT_PATH", raising=False)
    monkeypatch.delenv("LITELLM_LLM_BASE", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_ADMIN_KEY", raising=False)
    with pytest.raises(plr.RunnerError, match="litellm-base"):
        plr.main([
            "--data-dir", str(tmp_path),
            "--second-judge-dir", str(tmp_path),
            "--out-dir", str(tmp_path),
            "--prompt-path", str(tmp_path / "x.md"),
        ])
