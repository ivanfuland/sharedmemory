import pytest

from everos_adapter.batching import ensure_unique_timestamps, into_batches


def _m(ts):
    return {"sender_id": "a", "role": "user", "timestamp": ts, "content": "x"}


def test_duplicate_timestamps_bumped_to_next_free_ms():
    out = ensure_unique_timestamps([_m(100), _m(100), _m(100)])
    assert [m["timestamp"] for m in out] == [100, 101, 102]


def test_already_monotonic_untouched():
    out = ensure_unique_timestamps([_m(1), _m(5), _m(9)])
    assert [m["timestamp"] for m in out] == [1, 5, 9]


def test_out_of_order_is_forced_monotonic():
    out = ensure_unique_timestamps([_m(10), _m(3)])
    assert out[1]["timestamp"] == 11


def test_zero_timestamp_lifted_above_zero():
    # DTO 硬约束：timestamp 必须 > 0
    out = ensure_unique_timestamps([_m(0), _m(0)])
    assert all(m["timestamp"] > 0 for m in out)


def test_missing_ts_mixed_with_real_epoch():
    # codex R0 P0#3：reader 对缺失 created_at 返回 0；与真实 ms epoch 混排时
    # 0 必须被当缺失值顺延填补，不能被当真实 epoch 处理。
    out = ensure_unique_timestamps([_m(0), _m(1751961600300), _m(0)])
    assert [m["timestamp"] for m in out] == [1, 1751961600300, 1751961600301]


def test_large_same_ms_cluster_never_dropped():
    # 回归防护（2026-07-13 review finding）：曾有 cumulative-skew quarantine 机制，
    # ~347 条共享同一 ms-epoch 的消息就会被误判 skew 超限、整会话丢弃——违反
    # 「不丢数据」铁律。此机制已删除；本测试证明：数百条消息共享同一 ms-epoch 时
    # 一条都不丢，且分配的 ts 严格递增、互不相同、全部 > 0。若有人重新引入
    # 累计位移丢弃逻辑，本测试会失败。
    n = 400
    msgs = [_m(1_000_000) for _ in range(n)]
    out = ensure_unique_timestamps(msgs)
    assert len(out) == n  # 一条都没丢
    ts_list = [m["timestamp"] for m in out]
    assert all(t > 0 for t in ts_list)
    assert ts_list == sorted(ts_list)  # 严格递增
    assert len(set(ts_list)) == n  # 互不相同


def test_input_not_mutated():
    src = [_m(100), _m(100)]
    ensure_unique_timestamps(src)
    assert [m["timestamp"] for m in src] == [100, 100]


def test_into_batches_respects_dto_limit():
    msgs = [_m(i + 1) for i in range(1201)]
    batches = into_batches(msgs, size=500)
    assert [len(b) for b in batches] == [500, 500, 201]


def test_into_batches_exact_multiple_single_batch():
    msgs = [_m(i + 1) for i in range(500)]
    batches = into_batches(msgs, size=500)
    assert [len(b) for b in batches] == [500]


def test_into_batches_exact_multiple_two_batches():
    msgs = [_m(i + 1) for i in range(1000)]
    batches = into_batches(msgs, size=500)
    assert [len(b) for b in batches] == [500, 500]


def test_into_batches_rejects_oversize():
    with pytest.raises(ValueError):
        into_batches([_m(1)], size=501)


def test_into_batches_rejects_zero_size():
    with pytest.raises(ValueError):
        into_batches([_m(1)], size=0)


def test_into_batches_empty():
    assert into_batches([]) == []
