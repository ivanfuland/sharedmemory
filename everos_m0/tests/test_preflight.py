"""M0 preflight 闸的纯决策逻辑测试。

这个闸存在的全部理由：LiteLLM 默认 `max_budget=null`（不设限），拿这种 key 去驱动
EverOS 的写侧提炼（每 memcell ≥3-5 次 LLM）就是敞口。读不到 finite 预算就不许喂。
"""

from everos_m0.preflight import evaluate_budget_info, evaluate_infinity_probe


def test_infinite_budget_rejected():
    # LiteLLM /key/info: max_budget=None 表示不设限 → 必须拒
    ok, reason = evaluate_budget_info({"info": {"max_budget": None, "spend": 0.0}})
    assert ok is False and "max_budget" in reason


def test_over_spend_rejected():
    ok, reason = evaluate_budget_info({"info": {"max_budget": 5.0, "spend": 5.0}})
    assert ok is False and "spend" in reason


def test_finite_budget_with_headroom_ok():
    ok, reason = evaluate_budget_info({"info": {"max_budget": 5.0, "spend": 1.0}})
    assert ok is True


def test_flat_payload_without_info_wrapper():
    # /key/info 的真实响应是 {"key": ..., "info": {...}}；容忍已解包的形态
    ok, _ = evaluate_budget_info({"max_budget": 100.0, "spend": 0.0})
    assert ok is True


def test_missing_spend_treated_as_zero():
    ok, _ = evaluate_budget_info({"info": {"max_budget": 5.0}})
    assert ok is True


def test_infinity_probe_needs_both():
    ok, reason = evaluate_infinity_probe(embed_ok=True, rerank_ok=False)
    assert ok is False and "rerank" in reason
    ok, _ = evaluate_infinity_probe(embed_ok=True, rerank_ok=True)
    assert ok is True


def test_infinity_probe_embed_failure_named():
    ok, reason = evaluate_infinity_probe(embed_ok=False, rerank_ok=True)
    assert ok is False and "embedding" in reason
