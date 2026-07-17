"""probe_scores.py 的测试(P4/P5 §Task 4:打分底座 + known-control)。

核心断言(P0-2,codex 抓出的命门):Infinity `/rerank` 的 `results` 按分数降序返回,
不是按输入序;如果直接按返回顺序取值,会把分数错配到别的候选卡上——产出的
"哪张卡分高"结论全错但看起来跑得很正常。本文件把"故意乱序的 mock 响应"当
核心断言,不是边角用例:任何一个响应还原/index 闭合/缓存 fail-closed 测试失败,
都意味着下游判据引擎会吃到错配分数。

本任务只做 mock/合成 fixture(R4,P1-3):不接触任何真实分数,不发真实 HTTP
请求,不跑 live known-control(移到 Task 6 Step 5 第一阶段)。
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from everos_eval.probe_passage import PAIR_BUDGET, rerank_tokenizer
from everos_eval.probe_scores import (
    CACHE_META_FIELDS,
    KnownControlResult,
    ScoreCache,
    cached_call,
    cosine,
    embed,
    rerank,
    run_known_control_checks,
    select_known_control_cards,
)


# ======================================================================
# 测试用假响应对象(替代真实 urlopen 返回的 http.client.HTTPResponse)
# ======================================================================

class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeTokenizer:
    """受控 token 计数的假 tokenizer,避免大多数用例依赖本机 HF 缓存
    (只有专门验证"确实接了 Task 3 真 tokenizer"的用例才用 rerank_tokenizer())。
    每个 doc 的 token 数 = query 字符数 + doc 字符数 + 3(special tokens 近似)。"""

    def encode(self, query: str, doc: str = None, add_special_tokens: bool = True):
        if doc is None:
            return [0] * (len(query) + 2)
        return [0] * (len(query) + len(doc) + 3)


# ======================================================================
# embed():响应还原契约(P0-2)
# ======================================================================

def test_embed_scatters_out_of_order_response_back_to_input_index():
    # data 数组故意乱序返回(index=1 排前面),模拟 Infinity 不保证顺序的场景。
    payload = {
        "data": [
            {"index": 1, "embedding": [0.4, 0.5, 0.6]},
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
        ]
    }
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        vecs = embed(["text-a", "text-b"], base_url="http://fake", model="fake-model")
    assert vecs[0] == [0.1, 0.2, 0.3]
    assert vecs[1] == [0.4, 0.5, 0.6]


def test_embed_missing_index_raises():
    payload = {"data": [{"index": 0, "embedding": [0.1]}]}
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        with pytest.raises(ValueError, match="缺失"):
            embed(["a", "b"], base_url="http://fake", model="m")


def test_embed_duplicate_index_raises():
    payload = {
        "data": [
            {"index": 0, "embedding": [0.1]},
            {"index": 0, "embedding": [0.2]},
        ]
    }
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        with pytest.raises(ValueError, match="重复"):
            embed(["a", "b"], base_url="http://fake", model="m")


def test_embed_out_of_range_index_raises():
    payload = {
        "data": [
            {"index": 0, "embedding": [0.1]},
            {"index": 5, "embedding": [0.2]},
        ]
    }
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        with pytest.raises(ValueError, match="越界"):
            embed(["a", "b"], base_url="http://fake", model="m")


def test_embed_nan_component_raises():
    payload = {"data": [{"index": 0, "embedding": [float("nan"), 0.2]}]}
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        with pytest.raises(ValueError, match="非有限值"):
            embed(["a"], base_url="http://fake", model="m")


def test_embed_inconsistent_dimension_raises():
    payload = {
        "data": [
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            {"index": 1, "embedding": [0.1, 0.2]},
        ]
    }
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        with pytest.raises(ValueError, match="维度不一致"):
            embed(["a", "b"], base_url="http://fake", model="m")


def test_embed_empty_input_returns_empty_without_request():
    mock_urlopen = MagicMock()
    with patch("everos_eval.probe_scores.urlopen", mock_urlopen):
        assert embed([], base_url="http://fake", model="m") == []
    mock_urlopen.assert_not_called()


# ---- post_json 注入(everos_mcp 出站唯一通道复用探针底座,Task 2)----

def test_embed_injected_post_json_used_instead_of_default_urlopen():
    """注入 post_json 后必须走注入通道:调用计数 == 1,且默认 urlopen 全程零调用。"""
    calls = []

    def fake_post_json(url, payload, *, timeout=60):
        calls.append((url, payload, timeout))
        return {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}

    mock_urlopen = MagicMock()
    with patch("everos_eval.probe_scores.urlopen", mock_urlopen):
        vecs = embed(["text-a"], base_url="http://fake", model="m", timeout=7,
                     post_json=fake_post_json)
    assert vecs == [[0.1, 0.2]]
    assert len(calls) == 1
    assert calls[0] == ("http://fake/embeddings", {"input": ["text-a"], "model": "m"}, 7)
    mock_urlopen.assert_not_called()


def test_embed_without_injection_behavior_unchanged():
    """不传 post_json 时行为不变:仍走现行 _post_json/urlopen 路径。"""
    payload = {"data": [{"index": 0, "embedding": [0.3, 0.4]}]}
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)) as mock_urlopen:
        vecs = embed(["text-a"], base_url="http://fake", model="m")
    assert vecs == [[0.3, 0.4]]
    mock_urlopen.assert_called_once()


# ======================================================================
# rerank():响应还原契约(P0-2,核心断言——乱序 mock)
# ======================================================================

def test_rerank_scatters_score_sorted_response_back_to_input_index():
    """核心断言:Infinity 按分数降序返回 results(doc[2] 分最高排第一),
    必须按 item.index scatter 回输入序,不能假设 results[i] 对应 docs[i]。"""
    payload = {
        "results": [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.5},
            {"index": 1, "relevance_score": 0.1},
        ]
    }
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        scores = rerank("query", ["doc0", "doc1", "doc2"], base_url="http://fake",
                        model="m", tokenizer=_FakeTokenizer())
    assert scores == [0.5, 0.1, 0.9]


def test_rerank_missing_index_raises():
    payload = {"results": [{"index": 0, "relevance_score": 0.5}]}
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        with pytest.raises(ValueError, match="缺失"):
            rerank("q", ["d0", "d1"], base_url="http://fake", model="m",
                   tokenizer=_FakeTokenizer())


def test_rerank_duplicate_index_raises():
    payload = {
        "results": [
            {"index": 0, "relevance_score": 0.5},
            {"index": 0, "relevance_score": 0.6},
        ]
    }
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        with pytest.raises(ValueError, match="重复"):
            rerank("q", ["d0", "d1"], base_url="http://fake", model="m",
                   tokenizer=_FakeTokenizer())


def test_rerank_out_of_range_index_raises():
    payload = {
        "results": [
            {"index": 0, "relevance_score": 0.5},
            {"index": 9, "relevance_score": 0.6},
        ]
    }
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        with pytest.raises(ValueError, match="越界"):
            rerank("q", ["d0", "d1"], base_url="http://fake", model="m",
                   tokenizer=_FakeTokenizer())


def test_rerank_inf_score_raises():
    payload = {
        "results": [
            {"index": 0, "relevance_score": float("inf")},
            {"index": 1, "relevance_score": 0.1},
        ]
    }
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        with pytest.raises(ValueError, match="非有限值"):
            rerank("q", ["d0", "d1"], base_url="http://fake", model="m",
                   tokenizer=_FakeTokenizer())


def test_rerank_empty_docs_returns_empty_without_request():
    mock_urlopen = MagicMock()
    with patch("everos_eval.probe_scores.urlopen", mock_urlopen):
        assert rerank("q", [], base_url="http://fake", model="m",
                      tokenizer=_FakeTokenizer()) == []
    mock_urlopen.assert_not_called()


def test_rerank_http_500_raises_and_no_request_side_effects():
    err = HTTPError("http://fake/rerank", 500, "boom",
                     hdrs=None, fp=io.BytesIO(b'{"error":"server exploded"}'))
    with patch("everos_eval.probe_scores.urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="500"):
            rerank("q", ["d0"], base_url="http://fake", model="m",
                   tokenizer=_FakeTokenizer())


# ---- CE 对预算断言(Task 3 遗留点名):query+doc 对总 token 数超预算即拒 ----

def test_rerank_pair_over_budget_raises_before_any_request():
    class _HugeTokenizer:
        def encode(self, query, doc=None, add_special_tokens=True):
            if doc is None:
                return [0]
            return [0] * (PAIR_BUDGET + 1)  # 恰好超一个 token

    mock_urlopen = MagicMock()
    with patch("everos_eval.probe_scores.urlopen", mock_urlopen):
        with pytest.raises(ValueError, match="PAIR_BUDGET"):
            rerank("q", ["huge-doc"], base_url="http://fake", model="m",
                   tokenizer=_HugeTokenizer())
    mock_urlopen.assert_not_called()  # 超预算必须在发请求前拦下,不静默截断再发


def test_rerank_pair_within_budget_does_not_raise():
    class _TinyTokenizer:
        def encode(self, query, doc=None, add_special_tokens=True):
            if doc is None:
                return [0]
            return [0] * (PAIR_BUDGET - 1)

    payload = {"results": [{"index": 0, "relevance_score": 0.42}]}
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
        scores = rerank("q", ["short-doc"], base_url="http://fake", model="m",
                        tokenizer=_TinyTokenizer())
    assert scores == [0.42]


def test_rerank_default_tokenizer_is_task3_rerank_tokenizer():
    """不传 tokenizer 时必须落到 probe_passage.rerank_tokenizer()(Task 3 的真
    tokenizer)——验证"直接 import 用 Task 3 的 tokenizer"这条布线是对的,不是
    只在测试里用假 tokenizer 撑门面。"""
    payload = {"results": [{"index": 0, "relevance_score": 0.1}]}
    real_tokenizer = rerank_tokenizer()
    with patch("everos_eval.probe_scores.rerank_tokenizer",
               return_value=real_tokenizer) as mock_factory:
        with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)):
            rerank("你好", ["一段很短的中文文档"], base_url="http://fake", model="m")
    mock_factory.assert_called_once()


# ---- post_json 注入(everos_mcp 出站唯一通道复用探针底座,Task 2)----

def test_rerank_injected_post_json_used_instead_of_default_urlopen():
    calls = []

    def fake_post_json(url, payload, *, timeout=60):
        calls.append((url, payload, timeout))
        return {"results": [{"index": 0, "relevance_score": 0.77}]}

    mock_urlopen = MagicMock()
    with patch("everos_eval.probe_scores.urlopen", mock_urlopen):
        scores = rerank("q", ["d0"], base_url="http://fake", model="m", timeout=9,
                        tokenizer=_FakeTokenizer(), post_json=fake_post_json)
    assert scores == [0.77]
    assert len(calls) == 1
    assert calls[0] == ("http://fake/rerank", {"query": "q", "documents": ["d0"], "model": "m"}, 9)
    mock_urlopen.assert_not_called()


def test_rerank_without_injection_behavior_unchanged():
    payload = {"results": [{"index": 0, "relevance_score": 0.33}]}
    with patch("everos_eval.probe_scores.urlopen", return_value=_FakeResponse(payload)) as mock_urlopen:
        scores = rerank("q", ["d0"], base_url="http://fake", model="m",
                        tokenizer=_FakeTokenizer())
    assert scores == [0.33]
    mock_urlopen.assert_called_once()


# ======================================================================
# cosine()
# ======================================================================

def test_cosine_identical_vectors_is_one():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_vectors_is_negative_one():
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_dimension_mismatch_raises():
    with pytest.raises(ValueError, match="维度不一致"):
        cosine([1.0, 0.0], [1.0, 0.0, 0.0])


def test_cosine_zero_vector_raises():
    with pytest.raises(ValueError, match="零向量"):
        cosine([0.0, 0.0], [1.0, 0.0])


# ======================================================================
# ScoreCache:fail-closed 缓存 meta 纪律(P1-9/P1-11)
# ======================================================================

def _full_meta(**overrides) -> dict:
    meta = {field: f"{field}-value" for field in CACHE_META_FIELDS}
    meta["embedding_dim"] = 1024
    meta["cap"] = 2048
    meta["pair_budget"] = PAIR_BUDGET
    meta.update(overrides)
    return meta


def test_cache_construction_rejects_missing_field():
    meta = _full_meta()
    del meta["manifest_sha"]
    with pytest.raises(ValueError, match="缺字段"):
        ScoreCache(meta)


def test_cache_construction_rejects_unknown_value():
    meta = _full_meta(decoy_sha="unknown")
    with pytest.raises(ValueError, match="unknown"):
        ScoreCache(meta)


def test_cache_construction_rejects_empty_string_value():
    meta = _full_meta(code_git_sha="")
    with pytest.raises(ValueError, match="unknown"):
        ScoreCache(meta)


def test_cache_put_get_roundtrip_in_memory():
    cache = ScoreCache(_full_meta())
    assert cache.get("ce", "spec-sha", "synthetic", "q01", "ac_1") is None
    cache.put("ce", "spec-sha", "synthetic", "q01", "ac_1", 0.77)
    assert cache.get("ce", "spec-sha", "synthetic", "q01", "ac_1") == 0.77


def test_cache_persists_and_reloads_with_matching_meta(tmp_path: Path):
    path = tmp_path / "cache.json"
    meta = _full_meta()
    cache1 = ScoreCache(meta, path=path)
    cache1.put("ce", "spec-sha", "synthetic", "q01", "ac_1", 0.5)
    cache1.save()

    cache2 = ScoreCache(meta, path=path)
    assert cache2.rejected is False
    assert cache2.get("ce", "spec-sha", "synthetic", "q01", "ac_1") == 0.5


def test_cache_reload_with_mismatched_meta_is_rejected_wholesale(tmp_path: Path):
    path = tmp_path / "cache.json"
    meta = _full_meta()
    cache1 = ScoreCache(meta, path=path)
    cache1.put("ce", "spec-sha", "synthetic", "q01", "ac_1", 0.5)
    cache1.put("ce", "spec-sha", "synthetic", "q02", "ac_2", 0.6)
    cache1.save()

    drifted_meta = _full_meta(code_git_sha="different-sha")
    cache2 = ScoreCache(drifted_meta, path=path)
    assert cache2.rejected is True
    # 整批拒用:不是逐条 miss,是全部当不存在——两条都必须 miss
    assert cache2.get("ce", "spec-sha", "synthetic", "q01", "ac_1") is None
    assert cache2.get("ce", "spec-sha", "synthetic", "q02", "ac_2") is None


def test_cache_reload_with_corrupt_file_is_rejected_not_crash(tmp_path: Path):
    """缓存文件彻底损坏(非法 JSON)→ fail-closed 整批拒用,与 meta 不符同路径,
    不允许让整个 run 崩溃(损坏缓存只是"重算一遍"的代价,不是停工事由)。"""
    path = tmp_path / "cache.json"
    path.write_text('{"meta": {truncated garbage', encoding="utf-8")

    cache = ScoreCache(_full_meta(), path=path)
    assert cache.rejected is True
    assert cache.get("ce", "s", "synthetic", "q01", "ac_1") is None
    # 拒用后仍可正常工作(以空缓存起步)
    cache.put("ce", "s", "synthetic", "q01", "ac_1", 0.5)
    assert cache.get("ce", "s", "synthetic", "q01", "ac_1") == 0.5


def test_cache_reload_with_non_dict_top_level_is_rejected(tmp_path: Path):
    path = tmp_path / "cache.json"
    path.write_text('["legal json, wrong shape"]', encoding="utf-8")
    cache = ScoreCache(_full_meta(), path=path)
    assert cache.rejected is True


def test_cache_reload_with_unknown_value_in_stored_meta_is_rejected(tmp_path: Path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "meta": _full_meta(embed_model_revision="unknown"),
        "entries": {json.dumps(["ce", "spec-sha", "synthetic", "q01", "ac_1"]): 0.5},
    }), encoding="utf-8")

    cache = ScoreCache(_full_meta(), path=path)
    assert cache.rejected is True
    assert cache.get("ce", "spec-sha", "synthetic", "q01", "ac_1") is None


# ======================================================================
# cached_call():缓存命中不发请求 + 失败结果绝不落缓存(tombstone 纪律)
# ======================================================================

def test_cached_call_hit_does_not_invoke_compute_fn():
    cache = ScoreCache(_full_meta())
    compute_fn = MagicMock(return_value=0.9)
    v1 = cached_call(cache, signal="ce", spec_sha="s", variant="synthetic",
                     qid="q01", canonical_card_id="ac_1", compute_fn=compute_fn)
    assert v1 == 0.9
    compute_fn.assert_called_once()

    v2 = cached_call(cache, signal="ce", spec_sha="s", variant="synthetic",
                     qid="q01", canonical_card_id="ac_1", compute_fn=compute_fn)
    assert v2 == 0.9
    compute_fn.assert_called_once()  # 第二次仍是 1 次总调用——缓存命中不再调用


def test_cached_call_failure_propagates_and_leaves_no_residual_cache_entry():
    cache = ScoreCache(_full_meta())

    def _boom():
        raise RuntimeError("rerank HTTP 500: server exploded")

    with pytest.raises(RuntimeError, match="500"):
        cached_call(cache, signal="ce", spec_sha="s", variant="synthetic",
                   qid="q01", canonical_card_id="ac_1", compute_fn=_boom)

    # tombstone 纪律:失败结果绝不落缓存,后续必须仍是 miss(强制重算,不是永久空洞)
    assert cache.get("ce", "s", "synthetic", "q01", "ac_1") is None


def test_cached_call_end_to_end_with_rerank_avoids_second_http_call():
    """整合验证:第一次 cached_call 触发真实(mock)HTTP rerank 调用,第二次同 key
    命中缓存,urlopen 总调用次数必须仍是 1。"""
    cache = ScoreCache(_full_meta())
    payload = {"results": [{"index": 0, "relevance_score": 0.33}]}

    def _do_rerank():
        return rerank("q", ["doc"], base_url="http://fake", model="m",
                      tokenizer=_FakeTokenizer())[0]

    with patch("everos_eval.probe_scores.urlopen",
               return_value=_FakeResponse(payload)) as mock_urlopen:
        v1 = cached_call(cache, signal="ce", spec_sha="s", variant="synthetic",
                         qid="q01", canonical_card_id="ac_1", compute_fn=_do_rerank)
        v2 = cached_call(cache, signal="ce", spec_sha="s", variant="synthetic",
                         qid="q01", canonical_card_id="ac_1", compute_fn=_do_rerank)
    assert v1 == v2 == 0.33
    assert mock_urlopen.call_count == 1


# ======================================================================
# known-control:选卡规则(P5)
# ======================================================================

def _mk_candidate(cid: str, mem_type: str, native_score: float) -> dict:
    return {
        "canonical_card_id": cid,
        "mem_type": mem_type,
        "source_rank": 0,
        "native_score": native_score,
        "payload": {"id": cid},
    }


def _mk_gold(labels: dict, covered: set) -> dict:
    # 只造 primary 变体——select_known_control_cards/run_known_control_checks 只读 primary。
    return {"primary": {"labels": labels, "covered": covered}}


def test_select_known_control_picks_lowest_numbered_covered_query():
    candidates_by_qid = {
        "q02": [
            _mk_candidate("ac_b", "agent_case", 0.5),
            _mk_candidate("ac_a", "agent_case", 0.9),   # 字典序最小
            _mk_candidate("ac_c", "agent_case", 0.3),   # 同型 irrelevant,但字典序大于 ac_b
        ],
        "q05": [
            _mk_candidate("ac_x", "agent_case", 0.1),
        ],
    }
    labels = {
        ("q02", "ac_a"): {"relevant": True, "useful": True},
        ("q02", "ac_b"): {"relevant": False, "useful": False},
        ("q02", "ac_c"): {"relevant": False, "useful": False},
        ("q05", "ac_x"): {"relevant": True, "useful": True},
    }
    gold = _mk_gold(labels, covered={"q02", "q05"})

    selection = select_known_control_cards(gold, candidates_by_qid)

    assert selection.q_star == "q02"  # 字典序最小的 covered 查询,不是 q05
    assert selection.relevant["canonical_card_id"] == "ac_a"
    assert selection.same_type_irrelevant["canonical_card_id"] == "ac_b"  # ac_b < ac_c


def test_select_known_control_raises_when_no_covered_queries():
    gold = _mk_gold({}, covered=set())
    with pytest.raises(ValueError, match="covered"):
        select_known_control_cards(gold, {})


def test_select_known_control_raises_when_no_relevant_useful_candidate():
    candidates_by_qid = {"q01": [_mk_candidate("ac_a", "agent_case", 0.1)]}
    labels = {("q01", "ac_a"): {"relevant": False, "useful": False}}
    gold = _mk_gold(labels, covered={"q01"})
    with pytest.raises(ValueError, match="relevant"):
        select_known_control_cards(gold, candidates_by_qid)


def test_select_known_control_raises_when_no_same_type_irrelevant():
    candidates_by_qid = {
        "q01": [
            _mk_candidate("ac_a", "agent_case", 0.9),
            _mk_candidate("sk_a", "agent_skill", 0.1),
        ],
    }
    labels = {
        ("q01", "ac_a"): {"relevant": True, "useful": True},
        ("q01", "sk_a"): {"relevant": False, "useful": False},
    }
    gold = _mk_gold(labels, covered={"q01"})
    with pytest.raises(ValueError, match="同型"):
        select_known_control_cards(gold, candidates_by_qid)


# ======================================================================
# known-control:阻断断言 vs 非阻断诊断(P5)
# ======================================================================

def _mk_selection():
    from everos_eval.probe_scores import KnownControlSelection
    return KnownControlSelection(
        q_star="q01",
        relevant=_mk_candidate("ac_a", "agent_case", 0.9),
        same_type_irrelevant=_mk_candidate("ac_b", "agent_case", 0.1),
    )


def test_known_control_blocking_canonical_closure_failure_raises():
    selection = _mk_selection()
    with pytest.raises(AssertionError, match="canonical 闭合"):
        run_known_control_checks(
            selection,
            query_text="q",
            passages=["p-rel", "p-same-irr"],
            cards_ids={"ac_b"},  # 缺 ac_a
            gold_ids={"ac_a", "ac_b"},
            rerank_fn=lambda q, docs: [0.0] * len(docs),
            embed_fn=lambda texts: [[1.0, 0.0]] * len(texts),
            expected_native_scores={"ac_a": 0.9, "ac_b": 0.1},
        )


def test_known_control_blocking_native_score_mismatch_raises():
    selection = _mk_selection()
    with pytest.raises(AssertionError, match="native 分"):
        run_known_control_checks(
            selection,
            query_text="q",
            passages=["p-rel", "p-same-irr"],
            cards_ids={"ac_a", "ac_b"},
            gold_ids={"ac_a", "ac_b"},
            rerank_fn=lambda q, docs: [0.0] * len(docs),
            embed_fn=lambda texts: [[1.0, 0.0]] * len(texts),
            expected_native_scores={"ac_a": 0.9, "ac_b": 0.999},  # 台账不符
        )


def test_known_control_blocking_single_vs_batch_mismatch_raises():
    selection = _mk_selection()

    def _rerank_fn(query, docs):
        # batch 调用(2 docs)与单条调用(1 doc)刻意返回不一致的分数,模拟
        # index scatter 出 bug 的场景——这条断言正是为了抓这种坏 client。
        if len(docs) == 2:
            return [0.9, 0.1]
        return [0.5]  # 单条调用永远返回 0.5,与 batch 对不上

    with pytest.raises(AssertionError, match="单条 vs batch"):
        run_known_control_checks(
            selection,
            query_text="q",
            passages=["p-rel", "p-same-irr"],
            cards_ids={"ac_a", "ac_b"},
            gold_ids={"ac_a", "ac_b"},
            rerank_fn=_rerank_fn,
            embed_fn=lambda texts: [[1.0, 0.0]] * len(texts),
            expected_native_scores={"ac_a": 0.9, "ac_b": 0.1},
        )


def test_known_control_batch_drift_of_0p03_still_blocks():
    """容差灵敏度回归(仪器校准 2026-07-16):容差放宽到 0.02(实测良性批漂移
    上界 ~5e-3 的 4 倍)后,0.03 的差值仍必须阻断——防止未来有人继续放宽容差
    把真错位也放过去。"""
    selection = _mk_selection()

    def _rerank_fn(query, docs):
        if len(docs) == 2:
            return [0.9, 0.1]
        # 单条调用比 batch 高 0.03(> 0.02 容差,必须阻断)
        return [0.9 + 0.03] if docs[0] == "p-rel" else [0.1 + 0.03]

    with pytest.raises(AssertionError, match="单条 vs batch"):
        run_known_control_checks(
            selection,
            query_text="q",
            passages=["p-rel", "p-same-irr"],
            cards_ids={"ac_a", "ac_b"},
            gold_ids={"ac_a", "ac_b"},
            rerank_fn=_rerank_fn,
            embed_fn=lambda texts: [[1.0, 0.0]] * len(texts),
            expected_native_scores={"ac_a": 0.9, "ac_b": 0.1},
        )


def test_known_control_benign_batch_drift_within_tolerance_passes():
    """仪器校准正例:真实 Infinity 实测单条 vs batch 差 ~0.0049(cross-encoder
    padding/kernel 批组成数值效应,良性;换序对照已证明 scatter 正确、分数跟
    卡走)。0.005 量级的漂移必须放行,不再像 1e-4 时代那样把好管线拦停。"""
    selection = _mk_selection()

    def _rerank_fn(query, docs):
        if len(docs) == 2:
            return [0.9, 0.1]
        # 单条调用比 batch 高 0.0049(实测良性漂移上界,< 0.02 容差)
        return [0.9 + 0.0049] if docs[0] == "p-rel" else [0.1 + 0.0049]

    result = run_known_control_checks(
        selection,
        query_text="q",
        passages=["p-rel", "p-same-irr"],
        cards_ids={"ac_a", "ac_b"},
        gold_ids={"ac_a", "ac_b"},
        rerank_fn=_rerank_fn,
        embed_fn=lambda texts: [[1.0, 0.0], [0.9, 0.1], [0.1, 0.9]][:len(texts)],
        expected_native_scores={"ac_a": 0.9, "ac_b": 0.1},
    )
    assert isinstance(result, KnownControlResult)


def test_known_control_success_path_no_warnings_when_ordering_correct():
    selection = _mk_selection()

    def _rerank_fn(query, docs):
        # batch 与单条完全一致,且 relevant 卡分数 > same-type irrelevant 卡
        score_map = {"p-rel": 0.9, "p-same-irr": 0.1}
        return [score_map[d] for d in docs]

    def _embed_fn(texts):
        # query, p-rel, p-same-irr;cos(query,p-rel) 应高于 cos(query,p-same-irr)
        vec_map = {"q": [1.0, 0.0], "p-rel": [0.9, 0.1], "p-same-irr": [0.1, 0.9]}
        return [vec_map[t] for t in texts]

    result = run_known_control_checks(
        selection,
        query_text="q",
        passages=["p-rel", "p-same-irr"],
        cards_ids={"ac_a", "ac_b"},
        gold_ids={"ac_a", "ac_b"},
        rerank_fn=_rerank_fn,
        embed_fn=_embed_fn,
        expected_native_scores={"ac_a": 0.9, "ac_b": 0.1},
    )
    assert isinstance(result, KnownControlResult)
    assert result.warnings == []


def test_known_control_non_blocking_diagnostic_warns_but_does_not_raise():
    selection = _mk_selection()

    def _rerank_fn(query, docs):
        # ce 序反了:same-type irrelevant 分数比 relevant 还高
        score_map = {"p-rel": 0.1, "p-same-irr": 0.9}
        return [score_map[d] for d in docs]

    def _embed_fn(texts):
        # cos 序也反了
        vec_map = {"q": [1.0, 0.0], "p-rel": [0.1, 0.9], "p-same-irr": [0.9, 0.1]}
        return [vec_map[t] for t in texts]

    result = run_known_control_checks(
        selection,
        query_text="q",
        passages=["p-rel", "p-same-irr"],
        cards_ids={"ac_a", "ac_b"},
        gold_ids={"ac_a", "ac_b"},
        rerank_fn=_rerank_fn,
        embed_fn=_embed_fn,
        expected_native_scores={"ac_a": 0.9, "ac_b": 0.1},
    )
    # 非阻断:不抛异常,但两条诊断 warning 都应出现(cos 序反 + ce 序反)
    assert len(result.warnings) == 2
