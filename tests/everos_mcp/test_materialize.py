"""materialize.py 的测试(P4 Task 6:折叠规则 + 健康谓词 + 物化 DoD 指标)。

固定纪律(见 everos_mcp/materialize.py 顶部文档字符串,均为简报冻结项):
- healthy(scored_row, accepted_row):status=="ok" ∧ per_card 键集与 accepted
  candidates 编码键集严格相等(不多不少)∧ 全分数 finite ∧ pins 键集 ⊇
  PIN_KEYS 且值无 None/"unknown"。
- fold:健康行中 attempt_no 最大;无则最新 permanent_failure;再无则最新
  retryable_error;都没有 -> None。畸形 ok 行(缺卡/NaN/缺 pin)不进健康集,
  也不落进 permanent_failure/retryable_error 两个状态桶,因此不会被选中。
- materialize:只处理 traffic_class=="real" 的查询;DoD 反例①②必须给出
  确定性 dod_pass=False;H==0 时 dod_pass=False。
- CLI 输出路径必须落在 root 内,../ 逃逸拒绝。

三条流用 everos_mcp.ledger 的行构造器拼装,jsonl 直接写文件(不经过需要
flock 的 Ledger/LedgerWriter——iter_rows/read_abort_rids 本就是为这种只读
场景设计的 standalone 函数)。
"""
from __future__ import annotations

import json
import math
import stat
import subprocess
import sys
import time

import pytest

from everos_mcp import ledger, materialize


# ======================================================================
# 测试用小工具:直接拼三条流的 jsonl 文件
# ======================================================================

def _write_stream(root, name, rows):
    path = root / f"{name}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_aborts(root, rids):
    path = root / "aborts.log"
    with open(path, "a", encoding="utf-8") as f:
        for rid in rids:
            f.write(json.dumps({"rid": rid, "ts": time.time()}, ensure_ascii=False) + "\n")


def _candidate(card_type="case", card_id="c1", rank=0):
    return {
        "card_id": card_id,
        "card_type": card_type,
        "source_rank": rank,
        "native_score": 0.9,
        "payload_sha": "sha-" + card_id,
        "passage_sha": "psha-" + card_id,
        "truncated": False,
    }


def _hit_accepted(rid, ts=None, traffic_class="real", candidates=None):
    if candidates is None:
        candidates = [_candidate("case", "c1", 0), _candidate("skill", "s1", 1)]
    return ledger.accepted_row(
        "hit", rid, ts if ts is not None else time.time(), traffic_class,
        query="q", q_len=1, everos_rid="er-" + rid, candidates=candidates,
        returned_ids=[c["card_id"] for c in candidates],
        search_ms=5.0, pre_commit_ms=1.0, config_fp={"v": 1},
    )


def _full_pins():
    return {
        "embed_model": "bge-m3",
        "rerank_model": "bge-reranker",
        "model_artifact_fp": "fp1",
        "tokenizer_artifact_sha": "tok1",
        "infinity_image_digest": "sha256:abc",
        "embedding_dim": 1024,
        "uv_lock_sha": "uvsha1",
        "passage_spec_sha_case": "pscase1",
        "passage_spec_sha_skill": "psskill1",
        "cap": 8000,
        "query_budget": 20,
        "scorer_git_sha": "gitsha1",
    }


def _per_card(candidates):
    return {
        materialize._card_key(c): {"cos": 0.8, "ce": 0.7} for c in candidates
    }


def _scored(rid, producer, status, accepted, attempt_no, written_ts=None,
            per_card=None, pins=None, score_error_code=None):
    candidates = accepted.get("candidates", []) if accepted else []
    row = ledger.scored_row(
        rid, producer, status,
        per_card=per_card if per_card is not None else _per_card(candidates),
        pins=pins if pins is not None else _full_pins(),
        score_error_code=score_error_code,
    )
    row["attempt_no"] = attempt_no
    row["written_ts"] = written_ts if written_ts is not None else time.time()
    return row


def _full_query(root, rid, ts=None, traffic_class="real", healthy_scored=True,
                 producer="realtime", attempt_no=0):
    """搭一条完整的 real hit 查询:ops started+terminal + accepted(hit) +
    (可选)一条健康 scored 行。返回 accepted_row 供调用方进一步定制。"""
    ts = ts if ts is not None else time.time()
    _write_stream(root, "ops", [
        ledger.ops_started(rid, traffic_class),
        ledger.ops_terminal(rid, "hit"),
    ])
    accepted = _hit_accepted(rid, ts, traffic_class)
    _write_stream(root, "accepted", [accepted])
    if healthy_scored:
        _write_stream(root, "scored", [
            _scored(rid, producer, "ok", accepted, attempt_no)
        ])
    return accepted


# ======================================================================
# PIN_KEYS
# ======================================================================

def test_pin_keys_exact_twelve_keys():
    assert materialize.PIN_KEYS == frozenset({
        "embed_model", "rerank_model", "model_artifact_fp", "tokenizer_artifact_sha",
        "infinity_image_digest", "embedding_dim", "uv_lock_sha", "passage_spec_sha_case",
        "passage_spec_sha_skill", "cap", "query_budget", "scorer_git_sha",
    })
    assert len(materialize.PIN_KEYS) == 12


# ======================================================================
# healthy()
# ======================================================================

def test_healthy_true_for_well_formed_ok_row():
    accepted = _hit_accepted("r1")
    row = _scored("r1", "realtime", "ok", accepted, 0)
    assert materialize.healthy(row, accepted) is True


def test_healthy_false_status_not_ok():
    accepted = _hit_accepted("r1")
    row = _scored("r1", "realtime", "retryable_error", accepted, 0,
                   score_error_code="embed_timeout")
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_missing_card():
    accepted = _hit_accepted("r1")
    candidates = accepted["candidates"]
    per_card = _per_card(candidates)
    # 缺卡:删掉一个候选的打分
    del per_card[materialize._card_key(candidates[0])]
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_extra_card():
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    per_card["case:phantom"] = {"cos": 0.5, "ce": 0.5}
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_nan_score():
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    key = materialize._card_key(accepted["candidates"][0])
    per_card[key] = {"cos": math.nan, "ce": 0.7}
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_infinite_score():
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    key = materialize._card_key(accepted["candidates"][0])
    per_card[key] = {"cos": math.inf, "ce": 0.7}
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_per_card_value_none():
    """占位分数(值为 None)不能靠"没有不健康的数字"这种真空状态蒙混过关
    ——per-card 条目必须至少含一个数值叶子。"""
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    key = materialize._card_key(accepted["candidates"][0])
    per_card[key] = None
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_per_card_value_empty_dict():
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    key = materialize._card_key(accepted["candidates"][0])
    per_card[key] = {}
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_per_card_value_empty_list():
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    key = materialize._card_key(accepted["candidates"][0])
    per_card[key] = []
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_per_card_value_string():
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    key = materialize._card_key(accepted["candidates"][0])
    per_card[key] = "0.5"
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


def test_healthy_true_for_deeply_nested_finite_numerics():
    """分数值形状不必是扁平 float 或一层 {"cos":..,"ce":..}——只要递归展开
    后全是 finite 数值叶子,嵌套多深都算健康(_collect_numeric_leaves 对
    Task 7 尚未落地的真实形状保持中立)。"""
    accepted = _hit_accepted("r1")
    per_card = {
        materialize._card_key(c): {"scores": {"cos": [0.8], "ce": [0.7, 0.6]}}
        for c in accepted["candidates"]
    }
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is True


def test_healthy_false_nan_at_depth():
    accepted = _hit_accepted("r1")
    per_card = {
        materialize._card_key(c): {"scores": {"cos": [0.8], "ce": [math.nan]}}
        for c in accepted["candidates"]
    }
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_missing_pin():
    accepted = _hit_accepted("r1")
    pins = _full_pins()
    del pins["scorer_git_sha"]
    row = _scored("r1", "realtime", "ok", accepted, 0, pins=pins)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_pin_value_none():
    accepted = _hit_accepted("r1")
    pins = _full_pins()
    pins["cap"] = None
    row = _scored("r1", "realtime", "ok", accepted, 0, pins=pins)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_pin_value_unknown_string():
    accepted = _hit_accepted("r1")
    pins = _full_pins()
    pins["embed_model"] = "unknown"
    row = _scored("r1", "realtime", "ok", accepted, 0, pins=pins)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_empty_pins():
    accepted = _hit_accepted("r1")
    row = _scored("r1", "realtime", "ok", accepted, 0, pins={})
    assert materialize.healthy(row, accepted) is False


# ======================================================================
# fold()
# ======================================================================

def test_fold_prefers_max_attempt_no_among_healthy():
    accepted = _hit_accepted("r1")
    rows = [
        _scored("r1", "realtime", "ok", accepted, 0),
        _scored("r1", "reconciliation", "ok", accepted, 1),
    ]
    picked = materialize.fold(rows, accepted)
    assert picked is not None
    assert picked["attempt_no"] == 1


def test_fold_falls_back_to_latest_permanent_failure_when_no_healthy():
    accepted = _hit_accepted("r1")
    rows = [
        _scored("r1", "reconciliation", "permanent_failure", accepted, 0,
                score_error_code="embed_timeout"),
        _scored("r1", "reconciliation", "permanent_failure", accepted, 4,
                score_error_code="embed_timeout"),
        _scored("r1", "reconciliation", "retryable_error", accepted, 2,
                score_error_code="embed_timeout"),
    ]
    picked = materialize.fold(rows, accepted)
    assert picked is not None
    assert picked["status"] == "permanent_failure"
    assert picked["attempt_no"] == 4


def test_fold_falls_back_to_latest_retryable_error_when_nothing_else():
    accepted = _hit_accepted("r1")
    rows = [
        _scored("r1", "realtime", "retryable_error", accepted, 0, score_error_code="e1"),
        _scored("r1", "realtime", "retryable_error", accepted, 3, score_error_code="e1"),
    ]
    picked = materialize.fold(rows, accepted)
    assert picked is not None
    assert picked["status"] == "retryable_error"
    assert picked["attempt_no"] == 3


def test_fold_returns_none_for_empty_rows():
    accepted = _hit_accepted("r1")
    assert materialize.fold([], accepted) is None


def test_fold_skips_malformed_ok_rows_missing_card():
    """畸形 ok(缺卡)不健康,也不落 permanent_failure/retryable_error 桶,
    fold 必须整体返回 None(不能被误选中)。"""
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    del per_card[materialize._card_key(accepted["candidates"][0])]
    malformed = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.fold([malformed], accepted) is None


def test_fold_skips_malformed_ok_rows_nan():
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    key = materialize._card_key(accepted["candidates"][0])
    per_card[key] = {"cos": math.nan}
    malformed = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.fold([malformed], accepted) is None


def test_fold_skips_malformed_ok_rows_missing_pin():
    accepted = _hit_accepted("r1")
    pins = _full_pins()
    del pins["cap"]
    malformed = _scored("r1", "realtime", "ok", accepted, 0, pins=pins)
    assert materialize.fold([malformed], accepted) is None


def test_fold_prefers_healthy_over_permanent_failure_and_retryable():
    accepted = _hit_accepted("r1")
    rows = [
        _scored("r1", "reconciliation", "permanent_failure", accepted, 5,
                score_error_code="e1"),
        _scored("r1", "realtime", "ok", accepted, 0),
    ]
    picked = materialize.fold(rows, accepted)
    assert picked["status"] == "ok"
    assert picked["attempt_no"] == 0


# ======================================================================
# score_eligible()
# ======================================================================

def test_score_eligible_true_for_hit_with_candidates():
    accepted = _hit_accepted("r1")
    assert materialize.score_eligible("hit", accepted) is True


def test_score_eligible_false_for_non_hit():
    accepted = _hit_accepted("r1")
    assert materialize.score_eligible("abstain_empty", accepted) is False
    assert materialize.score_eligible("error", accepted) is False


def test_score_eligible_false_for_empty_candidates():
    accepted = ledger.accepted_row(
        "empty", "r1", time.time(), "real",
        query="q", q_len=1, everos_rid="er1", search_ms=1.0,
        pre_commit_ms=1.0, config_fp={"v": 1},
    )
    assert materialize.score_eligible("hit", accepted) is False


def test_score_eligible_false_for_none_accepted():
    assert materialize.score_eligible("hit", None) is False


# ======================================================================
# materialize(): join + DoD 指标
# ======================================================================

def test_materialize_basic_hit_counts_into_h_and_closure(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for i in range(30):
        _full_query(root, f"hit-{i}")
    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.total_real == 30
    assert stats.non_error_real == 30
    assert stats.H == 30
    assert stats.final_closure_count == 30
    assert stats.online_health_count == 30
    assert stats.error_rate == 0.0


def test_materialize_response_aborted_excludes_from_h(tmp_path):
    """late-abort:迟到的 hit 行落盘,但其后跟 response_aborted 事件行 ->
    effective_status=error,不进 H(打分分母),即便有健康 scored 行。"""
    root = tmp_path / "root"
    root.mkdir()
    rid = "late-abort-1"
    _write_stream(root, "ops", [
        ledger.ops_started(rid, "real"),
        ledger.ops_terminal(rid, "error", error_code="ledger_timeout"),
    ])
    accepted = _hit_accepted(rid)
    _write_stream(root, "accepted", [
        accepted,
        ledger.response_aborted_row(rid, "accepted_ack_timeout"),
    ])
    _write_stream(root, "scored", [_scored(rid, "realtime", "ok", accepted, 0)])

    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.total_real == 1
    assert stats.H == 0
    assert stats.non_error_real == 0
    assert stats.error_rate == 1.0


def test_materialize_dod_counter_example_1_error_rate_fails(tmp_path):
    """30 hit 健康 + 30 条只有 ops started+terminal(error,主账无行)
    -> error_rate=0.5 -> dod_pass=False(硬可用性门 <=20% 被打穿)。"""
    root = tmp_path / "root"
    root.mkdir()
    for i in range(30):
        _full_query(root, f"hit-{i}")
    for i in range(30):
        rid = f"ledgerfail-{i}"
        _write_stream(root, "ops", [
            ledger.ops_started(rid, "real"),
            ledger.ops_terminal(rid, "error", error_code="ledger_unavailable"),
        ])
        # 主账(accepted)无行——写失败场景

    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.total_real == 60
    assert stats.error_rate == pytest.approx(0.5)
    assert stats.dod_pass is False


def test_materialize_dod_counter_example_2_late_abort_not_in_h(tmp_path):
    """late-abort 行不进 H:即使 accepted 行是 hit、且有健康 scored 行,
    只要有 response_aborted 事件,该查询就不计入 H/暴露。"""
    root = tmp_path / "root"
    root.mkdir()
    for i in range(29):
        _full_query(root, f"hit-{i}")
    rid = "late-abort"
    _write_stream(root, "ops", [
        ledger.ops_started(rid, "real"),
        ledger.ops_terminal(rid, "error", error_code="ledger_timeout"),
    ])
    accepted = _hit_accepted(rid)
    _write_stream(root, "accepted", [
        accepted,
        ledger.response_aborted_row(rid, "accepted_ack_timeout"),
    ])
    _write_stream(root, "scored", [_scored(rid, "realtime", "ok", accepted, 0)])

    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.total_real == 30
    assert stats.H == 29
    assert stats.error_rate == pytest.approx(1 / 30)


def test_materialize_synthetic_bench_rows_excluded(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for i in range(30):
        _full_query(root, f"hit-{i}")
    for i in range(50):
        _full_query(root, f"bench-{i}", traffic_class="synthetic_bench")

    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.total_real == 30
    assert stats.H == 30
    with open(out, encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 30
    for line in lines:
        row = json.loads(line)
        assert row["traffic_class"] == "real"


def test_materialize_h_zero_dod_fail(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for i in range(30):
        rid = f"empty-{i}"
        _write_stream(root, "ops", [
            ledger.ops_started(rid, "real"),
            ledger.ops_terminal(rid, "abstain_empty"),
        ])
        _write_stream(root, "accepted", [
            ledger.accepted_row(
                "empty", rid, time.time(), "real",
                query="q", q_len=1, everos_rid="er", search_ms=1.0,
                pre_commit_ms=1.0, config_fp={"v": 1},
            )
        ])
    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.H == 0
    assert stats.dod_pass is False


def test_materialize_permanent_failure_and_orphan_counts(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    now = time.time()

    # 30 条健康闭合的作垫底样本(避免样本量/error_rate 干扰这条断言的可读性)
    for i in range(28):
        _full_query(root, f"hit-{i}")

    # permanent_failure:无健康行,但有 permanent_failure 终态
    pf_rid = "pf-1"
    _write_stream(root, "ops", [
        ledger.ops_started(pf_rid, "real"),
        ledger.ops_terminal(pf_rid, "hit"),
    ])
    pf_accepted = _hit_accepted(pf_rid)
    _write_stream(root, "accepted", [pf_accepted])
    _write_stream(root, "scored", [
        _scored(pf_rid, "reconciliation", "permanent_failure", pf_accepted, 5,
                score_error_code="embed_timeout")
    ])

    # orphan:score_eligible,无终态,age > 24h
    orphan_rid = "orphan-1"
    old_ts = now - 25 * 3600
    _write_stream(root, "ops", [
        ledger.ops_started(orphan_rid, "real"),
        ledger.ops_terminal(orphan_rid, "hit"),
    ])
    orphan_accepted = _hit_accepted(orphan_rid, ts=old_ts)
    _write_stream(root, "accepted", [orphan_accepted])
    # 无 scored 行 -> 无终态

    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.permanent_failure_count == 1
    assert stats.orphan_count == 1


def test_materialize_orphan_not_counted_when_fresh(tmp_path):
    """同样是无终态的 score_eligible 查询,但 age < 24h -> 不计 orphan
    (仍在正常重试窗口内)。"""
    root = tmp_path / "root"
    root.mkdir()
    for i in range(29):
        _full_query(root, f"hit-{i}")
    rid = "fresh-pending"
    _write_stream(root, "ops", [
        ledger.ops_started(rid, "real"),
        ledger.ops_terminal(rid, "hit"),
    ])
    accepted = _hit_accepted(rid, ts=time.time() - 60)
    _write_stream(root, "accepted", [accepted])
    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.orphan_count == 0


def test_materialize_online_health_excludes_reconciliation_only_rows(tmp_path):
    """在线健康完成率只数 producer=realtime 的健康行;最终闭合率任意
    producer 健康都算——同一条查询在两个指标下应给出不同计数。"""
    root = tmp_path / "root"
    root.mkdir()
    for i in range(29):
        _full_query(root, f"hit-{i}")
    rid = "reconciled-only"
    _write_stream(root, "ops", [
        ledger.ops_started(rid, "real"),
        ledger.ops_terminal(rid, "hit"),
    ])
    accepted = _hit_accepted(rid)
    _write_stream(root, "accepted", [accepted])
    _write_stream(root, "scored", [
        _scored(rid, "reconciliation", "ok", accepted, 3),
    ])
    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.H == 30
    assert stats.final_closure_count == 30  # 29 realtime + 1 reconciliation-healthy
    assert stats.online_health_count == 29  # reconciliation 那条不算在线健康


# ======================================================================
# 连续同 score_error_code 计数:成功重置,异因穿插不重置
# ======================================================================

def test_materialize_consecutive_same_error_code_streak(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    base_ts = time.time() - 1000

    rows_spec = [
        ("q1", "retryable_error", "A"),
        ("q2", "retryable_error", "A"),
        ("q3", "retryable_error", "B"),   # 异因穿插,不重置 A 的计数
        ("q4", "retryable_error", "A"),   # A 连续计到 3
        ("q5", "ok", None),               # 成功,重置全部
        ("q6", "retryable_error", "A"),
        ("q7", "retryable_error", "A"),
    ]
    for idx, (rid, status, code) in enumerate(rows_spec):
        ts = base_ts + idx * 10
        _write_stream(root, "ops", [
            ledger.ops_started(rid, "real"),
            ledger.ops_terminal(rid, "hit"),
        ])
        accepted = _hit_accepted(rid, ts=ts)
        _write_stream(root, "accepted", [accepted])
        row = _scored(rid, "realtime", status, accepted, 0,
                       written_ts=ts + 1, score_error_code=code)
        _write_stream(root, "scored", [row])

    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.max_consecutive_score_error_streak == 3


def test_materialize_consecutive_streak_zero_when_all_healthy(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for i in range(30):
        _full_query(root, f"hit-{i}")
    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.max_consecutive_score_error_streak == 0


# ======================================================================
# CLI: 路径逃逸拒绝 + 0600
# ======================================================================

def test_resolve_output_path_rejects_dotdot_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(materialize.OutputPathEscape):
        materialize._resolve_within_root(root, "../escape.jsonl")


def test_resolve_output_path_allows_relative_inside(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    resolved = materialize._resolve_within_root(root, "view.jsonl")
    assert resolved == (root / "view.jsonl").resolve()


def test_materialize_rejects_escaping_out_path(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")
    escaping_out = tmp_path / "outside.jsonl"
    with pytest.raises(materialize.OutputPathEscape):
        materialize.materialize(root, escaping_out)
    assert not escaping_out.exists()


def test_materialize_output_file_created_0600(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")
    out = root / "view.jsonl"
    materialize.materialize(root, out)
    mode = stat.S_IMODE(out.stat().st_mode)
    assert mode == 0o600


def test_cli_end_to_end_writes_view_and_rejects_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")

    result = subprocess.run(
        [sys.executable, "-m", "everos_mcp.materialize", str(root), "view.jsonl"],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=30,
    )
    assert result.returncode == 0, result.stderr
    out_path = root / "view.jsonl"
    assert out_path.exists()
    mode = stat.S_IMODE(out_path.stat().st_mode)
    assert mode == 0o600

    result2 = subprocess.run(
        [sys.executable, "-m", "everos_mcp.materialize", str(root), "../escape.jsonl"],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=30,
    )
    assert result2.returncode != 0
    assert not (tmp_path / "escape.jsonl").exists()
