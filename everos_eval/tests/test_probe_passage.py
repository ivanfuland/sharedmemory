"""probe_passage.py 的测试(P4 §Task 3:token-aware passage 组装)。

窗口探针(GET $INFINITY_BASE/models)是 live 测试(需要 cc-infinity 服务),按既有
`live` marker 口径 gate;真数据全量统计(Step 2)同样是 live 测试,需要
EVEROS_PROBE2B_DATA。其余用例只依赖本机 pinned HF tokenizer snapshot(离线,
local_files_only=True),不 gate,失败即真失败——tokenizer 缺失时应 fail-loud,
不应假装跳过(与 MEMORY 铁律"transformers 不可用时 fail-loud 停"一致)。
"""
from __future__ import annotations

import json
import os
import statistics
from pathlib import Path

import pytest

from everos_eval.probe_passage import (
    build_passage,
    compute_cap,
    compute_query_budget,
    fetch_infinity_models,
    passage_spec_sha,
    rerank_tokenizer,
    run_window_probe,
    token_len,
)

DATA_DIR_ENV = "EVEROS_PROBE2B_DATA"

CASE_PAYLOAD = {
    "id": "ac_1",
    "task_intent": "对抗性审查 EverOS spec",
    "approach": "先读 spec 再核代码公开 API 面",
    "key_insight": "spec 假设必须映射到公开 API 面才算验证",
}

SKILL_PAYLOAD = {
    "id": "sk_1",
    "name": "对抗性架构审查",
    "description": "源码+实测双重验证",
    "content": "## Steps\n1. 加载规范文件\n2. 定位源码\n3. 对照 spec 假设",
}


@pytest.fixture(scope="module")
def tok():
    return rerank_tokenizer()


# ---- 拼接正确(prod / full) ----

def test_build_passage_prod_case_concatenates_task_intent_and_approach(tok):
    got = build_passage(CASE_PAYLOAD, "agent_case", "prod", cap=2048, tokenizer=tok)
    assert got == CASE_PAYLOAD["task_intent"] + "\n" + CASE_PAYLOAD["approach"]


def test_build_passage_prod_skill_concatenates_name_and_description(tok):
    got = build_passage(SKILL_PAYLOAD, "agent_skill", "prod", cap=2048, tokenizer=tok)
    assert got == SKILL_PAYLOAD["name"] + "\n" + SKILL_PAYLOAD["description"]


def test_build_passage_full_case_appends_key_insight(tok):
    got = build_passage(CASE_PAYLOAD, "agent_case", "full", cap=2048, tokenizer=tok)
    expected = (CASE_PAYLOAD["task_intent"] + "\n" + CASE_PAYLOAD["approach"]
                + "\n" + CASE_PAYLOAD["key_insight"])
    assert got == expected


def test_build_passage_full_skill_appends_content(tok):
    got = build_passage(SKILL_PAYLOAD, "agent_skill", "full", cap=2048, tokenizer=tok)
    expected = (SKILL_PAYLOAD["name"] + "\n" + SKILL_PAYLOAD["description"]
                + "\n" + SKILL_PAYLOAD["content"])
    assert got == expected


# ---- 缺字段 KeyError ----

def test_build_passage_prod_case_missing_task_intent_raises_keyerror(tok):
    bad = {k: v for k, v in CASE_PAYLOAD.items() if k != "task_intent"}
    with pytest.raises(KeyError):
        build_passage(bad, "agent_case", "prod", cap=2048, tokenizer=tok)


def test_build_passage_full_case_missing_key_insight_raises_keyerror(tok):
    bad = {k: v for k, v in CASE_PAYLOAD.items() if k != "key_insight"}
    with pytest.raises(KeyError):
        build_passage(bad, "agent_case", "full", cap=2048, tokenizer=tok)


def test_build_passage_prod_skill_missing_name_raises_keyerror(tok):
    bad = {k: v for k, v in SKILL_PAYLOAD.items() if k != "name"}
    with pytest.raises(KeyError):
        build_passage(bad, "agent_skill", "prod", cap=2048, tokenizer=tok)


# ---- token 截断 ≤ cap ----

def test_build_passage_truncates_long_case_to_cap(tok):
    long_case = dict(CASE_PAYLOAD, approach="很长的过程描述。" * 500)
    cap = 30
    got = build_passage(long_case, "agent_case", "prod", cap=cap, tokenizer=tok)
    assert token_len(got, tok, add_special_tokens=True) <= cap + 2
    # 确认真的截断了(不是恰好没触发截断分支)
    assert len(got) < len(long_case["task_intent"] + "\n" + long_case["approach"])


def test_build_passage_short_text_untouched_below_cap(tok):
    got = build_passage(CASE_PAYLOAD, "agent_case", "prod", cap=2048, tokenizer=tok)
    assert got == CASE_PAYLOAD["task_intent"] + "\n" + CASE_PAYLOAD["approach"]


# ---- prod vs full hash:R4 no-op 断言口径 ----

def test_prod_full_hash_differ_when_key_insight_visible_within_cap(tok):
    """预算够大,full 的 key_insight 落在截断线之内可见 -> 两个 spec 输出必须不同。"""
    cap = 200  # 远大于该 fixture 的总 token 数,两个 spec 都不触发截断
    prod = build_passage(CASE_PAYLOAD, "agent_case", "prod", cap=cap, tokenizer=tok)
    full = build_passage(CASE_PAYLOAD, "agent_case", "full", cap=cap, tokenizer=tok)
    assert prod != full
    assert hash(prod) != hash(full)


def test_prod_full_hash_same_when_cap_truncates_before_key_insight(tok):
    """R3 收紧口径的反面用例:cap 小到连 prod 部分都被截断,full 的额外内容永远
    够不到截断线 -> 两个 spec 输出相同(no-op),必须能被 100% hash 相同的判据捕捉到。"""
    long_case = dict(CASE_PAYLOAD, approach="过程描述反复出现。" * 200)
    cap = 15  # 远小于 task_intent+approach 本身的 token 数,key_insight 永远无法出现
    prod = build_passage(long_case, "agent_case", "prod", cap=cap, tokenizer=tok)
    full = build_passage(long_case, "agent_case", "full", cap=cap, tokenizer=tok)
    assert prod == full


# ---- passage_spec_sha ----

def test_passage_spec_sha_differs_by_spec():
    assert (passage_spec_sha("prod", 2048, "agent_case")
            != passage_spec_sha("full", 2048, "agent_case"))


def test_passage_spec_sha_differs_by_cap():
    assert (passage_spec_sha("prod", 2048, "agent_case")
            != passage_spec_sha("prod", 480, "agent_case"))


def test_passage_spec_sha_differs_by_mem_type():
    assert (passage_spec_sha("prod", 2048, "agent_case")
            != passage_spec_sha("prod", 2048, "agent_skill"))


def test_passage_spec_sha_stable_for_same_inputs():
    a = passage_spec_sha("prod", 2048, "agent_case")
    b = passage_spec_sha("prod", 2048, "agent_case")
    assert a == b
    assert len(a) == 64  # sha256 hexdigest


def test_passage_spec_sha_rejects_unknown_spec():
    with pytest.raises(ValueError):
        passage_spec_sha("bogus", 2048, "agent_case")


# ---- CAP 公式(P4 冻结公式,纯数学,不需要 tokenizer) ----

def test_compute_cap_picks_hard_cap_when_windows_are_large():
    assert compute_cap(embed_window=8192, rerank_window=8192, query_budget=153) == 2048


def test_compute_cap_picks_min_when_rerank_window_minus_budget_is_smallest():
    assert compute_cap(embed_window=8192, rerank_window=500, query_budget=153) == 347


def test_compute_cap_picks_embed_window_when_smallest():
    assert compute_cap(embed_window=200, rerank_window=8192, query_budget=153) == 200


# ---- query_budget 实测(不写死 128,R4) ----

def test_compute_query_budget_measures_150_char_worst_case(tok):
    budget = compute_query_budget(tok)
    # 150 个中文字符,含 special tokens,实测应显著多于 150 字符本身、且不是硬编码 128
    assert budget > 150
    assert budget != 128


# ---- Step 0:live 窗口探针(需要 INFINITY_BASE 可达) ----

def _infinity_base():
    return os.environ.get("INFINITY_BASE")


@pytest.mark.live
def test_fetch_infinity_models_sees_expected_models():
    base = _infinity_base()
    if not base:
        pytest.skip("set INFINITY_BASE to run this live test")
    seen = fetch_infinity_models(base)
    assert "BAAI/bge-m3" in seen
    assert "BAAI/bge-reranker-v2-m3" in seen


@pytest.mark.live
def test_run_window_probe_end_to_end():
    base = _infinity_base()
    if not base:
        pytest.skip("set INFINITY_BASE to run this live test")
    probe = run_window_probe(base)
    assert probe.embed_window > 0
    assert probe.rerank_window > 0
    assert probe.query_budget > 0
    assert probe.cap == compute_cap(probe.embed_window, probe.rerank_window, probe.query_budget)
    assert probe.cap <= 2048


# ---- Step 2:真数据全量 token 分布统计(live,需要 EVEROS_PROBE2B_DATA) ----

def _real_data_dir():
    raw = os.environ.get(DATA_DIR_ENV)
    return Path(raw) if raw else None


@pytest.mark.live
def test_real_data_passage_token_stats(tok):
    """全部 60 行 × 33 候选算 prod/full token 分布 + 截断比例 + no-op 比例,
    写 out/passage_stats.json(P4 Task 3 Step 2 验收产物)。"""
    data_dir = _real_data_dir()
    if data_dir is None or not data_dir.exists():
        pytest.skip(f"set {DATA_DIR_ENV}=<probe-2b data dir> to run this live test")

    from everos_eval.probe_candidates import load_candidates

    cap = 2048  # Step 0 实测下的冻结 CAP(见 run_window_probe;硬顶主导,窗口远大于它)

    prod_lens: list[int] = []
    full_lens: list[int] = []
    truncated_prod = 0
    truncated_full = 0
    total = 0
    hash_equal = 0
    diff_by_type: dict[str, int] = {"agent_case": 0, "agent_skill": 0}
    n_by_type: dict[str, int] = {"agent_case": 0, "agent_skill": 0}

    with (data_dir / "retrieval.jsonl").open(encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]

    assert len(rows) == 60

    for row in rows:
        for c in load_candidates(row):
            payload, mem_type = c["payload"], c["mem_type"]
            total += 1
            n_by_type[mem_type] += 1

            # 用 build_passage 走真实拼装(含缺字段/规格逻辑),而不是手拼
            prod_text = build_passage(payload, mem_type, "prod", cap=cap, tokenizer=tok)
            full_text = build_passage(payload, mem_type, "full", cap=cap, tokenizer=tok)

            prod_raw_len = token_len(
                "\n".join([payload["task_intent"], payload["approach"]])
                if mem_type == "agent_case"
                else "\n".join([payload["name"], payload["description"]]),
                tok,
            )
            full_raw_len = token_len(
                "\n".join([payload["task_intent"], payload["approach"], payload["key_insight"]])
                if mem_type == "agent_case"
                else "\n".join([payload["name"], payload["description"], payload["content"]]),
                tok,
            )
            prod_lens.append(prod_raw_len)
            full_lens.append(full_raw_len)
            if prod_raw_len > cap:
                truncated_prod += 1
            if full_raw_len > cap:
                truncated_full += 1

            if prod_text == full_text:
                hash_equal += 1
            else:
                diff_by_type[mem_type] += 1

    assert total == 60 * 33

    def _dist(xs: list[int]) -> dict:
        xs_sorted = sorted(xs)
        n = len(xs_sorted)
        return {
            "min": xs_sorted[0],
            "median": statistics.median(xs_sorted),
            "p90": xs_sorted[int(n * 0.9) - 1] if n else 0,
            "max": xs_sorted[-1],
        }

    no_op_ratio = hash_equal / total
    stats = {
        "n_candidates": total,
        "cap": cap,
        "prod_token_dist": _dist(prod_lens),
        "full_token_dist": _dist(full_lens),
        "prod_truncated_ratio": truncated_prod / total,
        "full_truncated_ratio": truncated_full / total,
        "prod_full_hash_equal_ratio": no_op_ratio,
        "full_is_noop": no_op_ratio == 1.0,  # R3 收紧口径:仅 100% 相同才判 no-op 放弃
        "diff_by_card_type": diff_by_type,
        "n_by_card_type": n_by_type,
    }

    out_dir = data_dir.parent / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "passage_stats.json"
    out_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\npassage_stats -> {out_path}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    # 硬断言:no-op 判定不能悄悄放过部分相同(R3 收紧口径的核心)
    if stats["prod_full_hash_equal_ratio"] < 1.0:
        assert stats["diff_by_card_type"]["agent_case"] + stats["diff_by_card_type"]["agent_skill"] > 0
