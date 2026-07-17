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


def test_healthy_true_for_nested_finite_numerics_under_cos_and_ce():
    """card 值必须直接含 "cos"/"ce" 两个键(P1a),但这两个键各自的值仍可以是
    嵌套结构——只要递归展开后全是 finite 数值叶子(_collect_numeric_leaves
    对 cos/ce 各自的内部形状保持中立,不要求裸 float)。"""
    accepted = _hit_accepted("r1")
    per_card = {
        materialize._card_key(c): {"cos": [0.8], "ce": [0.7, 0.6]}
        for c in accepted["candidates"]
    }
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is True


def test_healthy_false_nan_at_depth_inside_ce():
    accepted = _hit_accepted("r1")
    per_card = {
        materialize._card_key(c): {"cos": [0.8], "ce": [math.nan]}
        for c in accepted["candidates"]
    }
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


# ---- P1a:card 值必须直接含 "cos" AND "ce" 两个键,不是"任意一个数值叶子" ----

def test_healthy_false_cos_only_missing_ce():
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    key = materialize._card_key(accepted["candidates"][0])
    per_card[key] = {"cos": 0.8}  # 只有 cos,缺 ce
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


def test_healthy_false_arbitrary_key_not_cos_or_ce():
    """`{"foo": 1}` 曾经因为"至少一个数值叶子"这条旧规则被判健康——旧规则
    只要求"存在任意数值叶子",不管键名是不是 cos/ce。P1a 收紧为必须直接是
    cos 和 ce 两个键。"""
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    key = materialize._card_key(accepted["candidates"][0])
    per_card[key] = {"foo": 1}
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is False


def test_healthy_true_cos_and_ce_both_finite():
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    key = materialize._card_key(accepted["candidates"][0])
    per_card[key] = {"cos": 0.42, "ce": 0.13}
    row = _scored("r1", "realtime", "ok", accepted, 0, per_card=per_card)
    assert materialize.healthy(row, accepted) is True


def test_healthy_false_ce_nan():
    accepted = _hit_accepted("r1")
    per_card = _per_card(accepted["candidates"])
    key = materialize._card_key(accepted["candidates"][0])
    per_card[key] = {"cos": 0.8, "ce": math.nan}
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
# P2(R4 #2):物化行必须是"一查询一行的标定输入",不能只有 status/health/
# fold 元数据——hit 行须携带全部标定字段,早期 error 行(contract_reject)按
# 判别联合语义省去 candidates 相关字段。
# ======================================================================

def test_materialize_hit_row_carries_full_calibration_input(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    accepted = _full_query(root, "hit-1")
    out = root / "view.jsonl"
    materialize.materialize(root, out)
    with open(out, encoding="utf-8") as f:
        row = json.loads(f.readline())

    assert row["rid"] == "hit-1"
    assert row["effective_status"] == "hit"
    assert row["query"] == accepted["query"]
    assert row["q_len"] == accepted["q_len"]
    assert row["ts"] == pytest.approx(accepted["ts"])
    assert row["search_ms"] == accepted["search_ms"]
    assert row["config_fp"] == accepted["config_fp"]
    assert row["candidates"] == accepted["candidates"]
    assert row["returned_ids"] == accepted["returned_ids"]
    assert "error_code" not in row  # hit 行本就没有 error_code

    # 折叠选中的 scored attempt 标定字段(_full_query 默认写一条健康 realtime 行)
    assert set(row["per_card"].keys()) == {materialize._card_key(c) for c in accepted["candidates"]}
    assert row["pins"] == _full_pins()
    assert row["producer"] == "realtime"
    assert row["attempt_no"] == 0
    assert "score_error_code" not in row  # 健康行没有 score_error_code


def test_materialize_contract_reject_row_omits_candidate_fields(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    rid = "reject-1"
    _write_stream(root, "ops", [
        ledger.ops_started(rid, "real"),
        ledger.ops_terminal(rid, "error", error_code="task_empty"),
    ])
    accepted = ledger.accepted_row(
        "contract_reject", rid, time.time(), "real",
        error_code="task_empty", pre_commit_ms=1.0, config_fp={"v": 1},
    )
    _write_stream(root, "accepted", [accepted])

    out = root / "view.jsonl"
    materialize.materialize(root, out)
    with open(out, encoding="utf-8") as f:
        row = json.loads(f.readline())

    assert row["effective_status"] == "error"
    assert row["error_code"] == "task_empty"
    assert row["config_fp"] == {"v": 1}
    assert row["query"] is None  # contract_reject 的 query 是固定字段 None(存在但值为 None)
    # 判别联合语义:contract_reject 的 accepted 行里这些字段本就缺席,物化行
    # 也必须缺席,不能用 None/[] 占位假装"有这个字段"。
    for key in ("q_len", "search_ms", "candidates", "returned_ids",
                "per_card", "pins", "producer", "attempt_no", "score_error_code"):
        assert key not in row


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


def test_materialize_dod_fails_with_one_orphan_even_if_other_gates_pass(tmp_path):
    """P1b:dod_pass 必须并入 orphan 门(orphan_count==0)——此前该谓词漏了这
    一项,其余四门(样本量/error_rate/在线健康率/最终闭合率)即使全过,只要
    存在一条 >24h 未终态的 score_eligible 查询,dod_pass 也必须是 False。

    构造:99 条健康 hit(H 分母的绝大多数)+ 1 条 accepted.ts 在 25 小时前、
    从未落 scored 行的 hit 查询——online_health_rate=99/100=0.99>=0.90、
    final_closure_rate=99/100=0.99>=ceil(0.99*100)/100(边界恰好达标)、
    error_rate=0、样本量/streak 都不是瓶颈,唯独 orphan_count=1。"""
    root = tmp_path / "root"
    root.mkdir()
    for i in range(99):
        _full_query(root, f"hit-{i}")

    rid = "orphan-old"
    old_ts = time.time() - 25 * 3600  # 超过 24h orphan 门槛
    _write_stream(root, "ops", [
        ledger.ops_started(rid, "real"),
        ledger.ops_terminal(rid, "hit"),
    ])
    accepted = _hit_accepted(rid, ts=old_ts)
    _write_stream(root, "accepted", [accepted])
    # 故意不写任何 scored 行——这是待补打的孤儿。

    out = root / "view.jsonl"
    stats = materialize.materialize(root, out)
    assert stats.H == 100
    assert stats.orphan_count == 1
    assert stats.online_health_rate >= 0.90
    assert stats.final_closure_count >= 99  # 其余四门(样本/error_rate/在线/闭合)全过
    assert stats.error_rate == 0.0
    assert stats.dod_pass is False


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


# ======================================================================
# P1(阻断项):物化输出不得与账本自身占用的保留名/保留目录同名——containment
# 校验只挡 `../` 逃逸,`out_name="ops.jsonl"` 这类落在 root 内部的名字会
# 通过 containment 检查,但随后 O_TRUNC 写入会摧毁权威 ops 流(或其他账本
# 源文件)。见 everos_mcp/materialize.py `_resolve_within_root`。
# ======================================================================

_RESERVED_LEDGER_BASENAMES = (
    "ops.jsonl", "accepted.jsonl", "scored.jsonl",
    "aborts.log", "meta.json", "meta.lock", ".lock",
)


@pytest.mark.parametrize("reserved_name", _RESERVED_LEDGER_BASENAMES)
def test_resolve_output_path_rejects_reserved_ledger_basenames(tmp_path, reserved_name):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(materialize.OutputPathReserved):
        materialize._resolve_within_root(root, reserved_name)


@pytest.mark.parametrize("sealed_name", [
    "ops.sealed-1700000000-abcd1234.jsonl",
    "accepted.sealed-1-x.jsonl",
    "scored.sealed-9999999999-ffffffff.jsonl",
])
def test_resolve_output_path_rejects_sealed_segment_pattern(tmp_path, sealed_name):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(materialize.OutputPathReserved):
        materialize._resolve_within_root(root, sealed_name)


@pytest.mark.parametrize("reserved_subpath", [
    "blobs/deadbeef",
    "veccache/somefile",
])
def test_resolve_output_path_rejects_reserved_subdirs(tmp_path, reserved_subpath):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(materialize.OutputPathReserved):
        materialize._resolve_within_root(root, reserved_subpath)


def test_resolve_output_path_allows_benign_name_still(tmp_path):
    """回归护栏:保留名 denylist 不能误伤正常物化输出名。"""
    root = tmp_path / "root"
    root.mkdir()
    resolved = materialize._resolve_within_root(root, "materialized.jsonl")
    assert resolved == (root / "materialized.jsonl").resolve()


@pytest.mark.parametrize("reserved_name", _RESERVED_LEDGER_BASENAMES)
def test_materialize_rejects_reserved_name_and_source_bytes_unchanged(tmp_path, reserved_name):
    """回归本身要挡的洞:曾经 `out_name="ops.jsonl"` 能通过 containment 校验,
    随后 `_write_jsonl_0600` 的 `O_TRUNC` 直接摧毁权威 ops 流。这里先写入
    已知字节内容(账本自己产出的真实内容,或人工种下的哨兵字节),再尝试
    以该保留名 materialize,断言:① raise OutputPathReserved;② 目标文件的
    字节相对尝试前逐字节不变(这是审查者要求的回归断言,不能只测"抛异常"
    ——必须证明"源文件真的没被碰")。"""
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")  # 产出真实 ops.jsonl/accepted.jsonl/scored.jsonl 内容

    target = root / reserved_name
    if not target.exists():
        # meta.json / meta.lock / .lock / aborts.log 不是 _full_query 的产物,
        # 种一份已知哨兵内容,保证"有東西可被破坏"。
        target.write_bytes(b"SENTINEL-DO-NOT-TRUNCATE\n" * 3)
    before = target.read_bytes()
    assert before  # 前提:确实有非空内容,否则"字节不变"这个断言没有意义

    with pytest.raises(materialize.OutputPathReserved):
        materialize.materialize(root, reserved_name)

    after = target.read_bytes()
    assert after == before, (
        f"{reserved_name} 的字节在被拒绝的 materialize 尝试后发生了变化——"
        "O_TRUNC 破坏了账本源文件"
    )


def test_materialize_rejects_sealed_segment_name_and_source_bytes_unchanged(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")

    sealed_name = "ops.sealed-1700000000-abcd1234.jsonl"
    target = root / sealed_name
    target.write_bytes(b"SEALED-SEGMENT-SENTINEL\n" * 2)
    before = target.read_bytes()

    with pytest.raises(materialize.OutputPathReserved):
        materialize.materialize(root, sealed_name)

    after = target.read_bytes()
    assert after == before


def test_materialize_rejects_output_inside_blobs_dir(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")
    (root / "blobs").mkdir()

    with pytest.raises(materialize.OutputPathReserved):
        materialize.materialize(root, "blobs/whatever.jsonl")
    assert not (root / "blobs" / "whatever.jsonl").exists()


def test_materialize_rejects_output_inside_veccache_dir(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")
    (root / "veccache").mkdir()

    with pytest.raises(materialize.OutputPathReserved):
        materialize.materialize(root, "veccache/whatever.jsonl")
    assert not (root / "veccache" / "whatever.jsonl").exists()


def test_materialize_still_works_for_benign_out_name(tmp_path):
    """回归护栏:保留名拦截不能误伤正常物化流程。"""
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")
    out = root / "materialized_view.jsonl"
    stats = materialize.materialize(root, out)
    assert out.exists()
    assert stats.total_real == 1


def test_cli_rejects_reserved_ledger_name(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")
    before = (root / "ops.jsonl").read_bytes()

    result = subprocess.run(
        [sys.executable, "-m", "everos_mcp.materialize", str(root), "ops.jsonl"],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=30,
    )
    assert result.returncode != 0
    after = (root / "ops.jsonl").read_bytes()
    assert after == before


# ======================================================================
# P1(第二轮外部审查):保留名校验必须查相对路径的每一段 component,不能
# 只查最终 basename——`out_name="aborts.log/view.jsonl"` 的 basename 是
# 无害的 `view.jsonl`,但父目录段 `aborts.log` 若被当成目录名创建,会
# 摧毁 ledger.py `mark_abort()` 期望在那里的普通文件(此后 O_APPEND 写
# 永远失败,因为 open() 一个目录会报 IsADirectoryError)。
# ======================================================================

def test_resolve_output_path_rejects_reserved_name_as_intermediate_dir(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(materialize.OutputPathReserved):
        materialize._resolve_within_root(root, "aborts.log/view.jsonl")


def test_resolve_output_path_rejects_reserved_basename_as_intermediate_dir(tmp_path):
    """同类洞的另一变体:保留名出现在中间段,而不是最终 basename 那一段。"""
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(materialize.OutputPathReserved):
        materialize._resolve_within_root(root, "ops.jsonl/x/y.jsonl")


def test_resolve_output_path_allows_nested_benign_subdir(tmp_path):
    """回归护栏:多段路径本身不是问题,只有段命中保留名/保留子目录才拒绝。"""
    root = tmp_path / "root"
    root.mkdir()
    resolved = materialize._resolve_within_root(root, "sub/dir/view.jsonl")
    assert resolved == (root / "sub" / "dir" / "view.jsonl").resolve()


def test_materialize_rejects_aborts_log_as_intermediate_dir_and_no_directory_created(tmp_path):
    """端到端复现审查者描述的具体绕过路径:`out_name="aborts.log/view.jsonl"`
    在只查 basename 的旧实现里能通过校验,`_write_jsonl_0600` 的
    `path.parent.mkdir(parents=True, ...)` 就会把 `aborts.log` 创建成一个
    目录——这里断言修复后:① raise OutputPathReserved;② `aborts.log` 完全
    没有被创建(既不是文件也不是目录),不给 ledger 的 mark_abort() 留任何
    隐患。"""
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")
    aborts_path = root / "aborts.log"
    assert not aborts_path.exists()  # 前提:_full_query 不写 aborts.log

    with pytest.raises(materialize.OutputPathReserved):
        materialize.materialize(root, "aborts.log/view.jsonl")

    assert not aborts_path.exists(), "aborts.log 不应被创建成目录(或任何东西)"


def test_materialize_rejects_ops_jsonl_as_intermediate_dir_and_source_untouched(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")
    ops_path = root / "ops.jsonl"
    before = ops_path.read_bytes()

    with pytest.raises(materialize.OutputPathReserved):
        materialize.materialize(root, "ops.jsonl/x/y.jsonl")

    assert ops_path.is_file()  # 没被 mkdir 变成目录
    assert ops_path.read_bytes() == before
    assert not (root / "ops.jsonl" / "x").exists()


def test_materialize_still_works_for_nested_benign_out_path(tmp_path):
    """回归护栏:嵌套的良性子目录输出路径必须继续可用(不能因为拦截逃逸
    路径而误伤"物化输出想放进子目录"这个正常用例)。"""
    root = tmp_path / "root"
    root.mkdir()
    _full_query(root, "hit-1")
    out = root / "views" / "materialized.jsonl"
    stats = materialize.materialize(root, "views/materialized.jsonl")
    assert out.exists()
    assert stats.total_real == 1
