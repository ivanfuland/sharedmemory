"""ledger.py 的测试(P4 Task 4:三条流 writer + 崩溃协议 + flock)。

固定纪律(见 everos_mcp/ledger.py 顶部文档字符串):
- submit() 回执在 fsync 之后才发出;超时 -> LedgerTimeout,行仍在队列里,
  writer 恢复后照写(late-commit),FIFO 顺序保持。
- 双实例 flock -> LedgerLocked。
- 残尾(尾部非完整行) -> 整文件 rename 为 sealed-*,新开段;iter_rows 跨段
  读齐。
- 32 线程并发 submit 1k 行 -> 每行都是合法 JSON,无交错。
- effective_status:优先级链、判别联合"无伪字段"。
- scored-writer 独有职责:attempt_no 串行分配 + 跨重启恢复、validator 在
  写入时机改写不健康的 ok 行。
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest

from everos_mcp import ledger


# ======================================================================
# submit() 回执语义:receipt-after-fsync + timeout 后 late-commit + FIFO
# ======================================================================

def test_receipt_after_fsync_and_timeout_leaves_row_queued(tmp_path, monkeypatch):
    writer = ledger.LedgerWriter(tmp_path / "ops.jsonl", "ops")

    real_fsync = os.fsync
    release = threading.Event()
    entered_fsync = threading.Event()

    def blocking_fsync(fd):
        entered_fsync.set()
        release.wait(timeout=10)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", blocking_fsync)

    row1 = {"rid": "r1", "kind": "started", "ts": 1.0}
    with pytest.raises(ledger.LedgerTimeout):
        writer.submit(row1, timeout=0.3)

    assert entered_fsync.is_set()

    # 迟到的 response_aborted 在超时之后才提交——FIFO 顺序上必须排在 row1 后面。
    row2 = {"rid": "r1", "kind": "response_aborted", "ts": 2.0}

    def submit_row2():
        writer.submit(row2, timeout=10)

    t = threading.Thread(target=submit_row2)
    t.start()
    time.sleep(0.2)  # 让 row2 真的排到队列里,而不是在 fsync 放开之后才提交
    release.set()
    t.join(timeout=10)
    assert not t.is_alive()

    writer.close(drain=True)

    lines = (tmp_path / "ops.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["kind"] == "started"
    assert json.loads(lines[1])["kind"] == "response_aborted"


def test_submit_nowait_does_not_block_and_preserves_fifo(tmp_path):
    """P2(R4 阻断项 #7):`submit_nowait` 是 fire-and-forget——排队立即返回,
    不等待任何回执。用一个人为阻塞的 fsync 撑住 writer,验证:①调用本身
    几乎瞬时返回(不像 `submit()` 那样卡到 timeout);②行最终仍然按 FIFO
    顺序落盘(与前面已经排队的 `submit()` 行顺序一致,不会因为是 nowait 就
    插队或乱序)。"""
    writer = ledger.LedgerWriter(tmp_path / "ops.jsonl", "ops")

    real_fsync = os.fsync
    release = threading.Event()
    entered_fsync = threading.Event()

    def blocking_fsync(fd):
        entered_fsync.set()
        release.wait(timeout=10)
        return real_fsync(fd)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(os, "fsync", blocking_fsync)
    try:
        row1 = {"rid": "r1", "kind": "started", "ts": 1.0}

        def submit_row1():
            writer.submit(row1, timeout=10)

        t = threading.Thread(target=submit_row1)
        t.start()
        assert entered_fsync.wait(timeout=5)  # row1 已经在 writer 线程里卡住

        row2 = {"rid": "r1", "kind": "response_aborted", "ts": 2.0}
        t0 = time.monotonic()
        writer.submit_nowait(row2)  # 不应该阻塞——writer 还卡在 row1 的 fsync 里
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0

        release.set()
        t.join(timeout=10)
        assert not t.is_alive()
    finally:
        monkeypatch.undo()

    writer.close(drain=True)

    lines = (tmp_path / "ops.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["kind"] == "started"
    assert json.loads(lines[1])["kind"] == "response_aborted"


def test_write_failure_raises_immediately_not_timeout(tmp_path):
    writer = ledger.LedgerWriter(tmp_path / "accepted.jsonl", "accepted", fault_reason="accepted_write_fail")
    with pytest.raises(ledger.LedgerUnavailable):
        writer.submit({"rid": "r1"}, timeout=5.0)
    writer.close(drain=True)
    assert (tmp_path / "accepted.jsonl").read_text(encoding="utf-8") == ""


# ======================================================================
# flock
# ======================================================================

def test_flock_second_instance_fails(tmp_path):
    led1 = ledger.Ledger(tmp_path / "root")
    try:
        with pytest.raises(ledger.LedgerLocked):
            ledger.Ledger(tmp_path / "root")
    finally:
        led1.close()


def test_flock_released_after_close_allows_new_instance(tmp_path):
    led1 = ledger.Ledger(tmp_path / "root")
    led1.close()
    led2 = ledger.Ledger(tmp_path / "root")
    led2.close()


# ======================================================================
# 残尾 / sealed 段
# ======================================================================

def test_torn_tail_seals_segment(tmp_path):
    # 手动模拟"重启前已存在的账目录"(而不是让 Ledger 自己新建)——权限必须
    # 显式摆成合规值,因为 Ledger 对已存在的目录/文件只校验、不修复。
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    ops_path = root / "ops.jsonl"
    good_row = {"rid": "r1", "kind": "started", "ts": 1.0}
    ops_path.write_text(json.dumps(good_row) + "\n" + '{"rid": "r2", "kind": "star', encoding="utf-8")

    led = ledger.Ledger(root)
    try:
        sealed = list(root.glob("ops.sealed-*.jsonl"))
        assert len(sealed) == 1
        assert sealed[0].read_text(encoding="utf-8").endswith('"kind": "star')

        # 新段是空的、存在、0600
        assert ops_path.exists()
        assert ops_path.read_text(encoding="utf-8") == ""
        assert stat.S_IMODE(ops_path.stat().st_mode) == 0o600

        # 写一条到新段,iter_rows 应该把 sealed 段的完整行 + 新段的行按序拼起来
        led.ops.submit({"rid": "r3", "kind": "started", "ts": 3.0}, timeout=5.0)
        rows, warnings = ledger.iter_rows(root, "ops")
        assert warnings == 1  # sealed 段里的半行
        assert [r["rid"] for r in rows] == ["r1", "r3"]
    finally:
        led.close()


def test_no_torn_tail_no_seal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    ops_path = root / "ops.jsonl"
    ops_path.write_text(json.dumps({"rid": "r1", "kind": "started"}) + "\n", encoding="utf-8")
    os.chmod(ops_path, 0o600)  # 不是残尾,不会被重新封段——权限得手动摆合规

    led = ledger.Ledger(root)
    try:
        assert list(root.glob("ops.sealed-*.jsonl")) == []
    finally:
        led.close()


def test_torn_tail_multiple_streams_sealed_independently(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    (root / "ops.jsonl").write_text('{"rid": "broken', encoding="utf-8")  # 残尾,会被重新封段,权限不用摆
    accepted_path = root / "accepted.jsonl"
    accepted_path.write_text(json.dumps({"rid": "r1"}) + "\n", encoding="utf-8")
    os.chmod(accepted_path, 0o600)  # 不是残尾,权限得手动摆合规

    led = ledger.Ledger(root)
    try:
        assert len(list(root.glob("ops.sealed-*.jsonl"))) == 1
        assert list(root.glob("accepted.sealed-*.jsonl")) == []
    finally:
        led.close()


# ======================================================================
# 并发提交,无交错
# ======================================================================

def test_concurrent_submit_no_interleave(tmp_path):
    writer = ledger.LedgerWriter(tmp_path / "ops.jsonl", "ops")
    n_threads = 32
    rows_per_thread = 1000 // n_threads  # ~31 每线程,凑够 >=992 行,断言用实际总数

    errors: list[Exception] = []

    def worker(idx):
        for i in range(rows_per_thread):
            try:
                writer.submit({"rid": f"t{idx}-{i}", "kind": "started", "ts": i}, timeout=10)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    writer.close(drain=True)

    assert errors == []
    lines = (tmp_path / "ops.jsonl").read_text(encoding="utf-8").splitlines()
    total_expected = n_threads * rows_per_thread
    assert len(lines) == total_expected
    seen_rids = set()
    for line in lines:
        row = json.loads(line)  # 每行必须是合法完整 JSON——交错会让这里炸
        assert row["rid"] not in seen_rids
        seen_rids.add(row["rid"])
    assert len(seen_rids) == total_expected


# ======================================================================
# effective_status
# ======================================================================

def test_effective_status_missing_terminal_is_error():
    ops_rows = [{"rid": "r1", "kind": "started", "ts": 1.0}]
    assert ledger.effective_status(ops_rows, [], set(), "r1") == "error"


def test_effective_status_no_started_at_all_is_error():
    assert ledger.effective_status([], [], set(), "r1") == "error"


def test_effective_status_hit_terminal():
    ops_rows = [
        {"rid": "r1", "kind": "started", "ts": 1.0},
        {"rid": "r1", "kind": "terminal", "ts": 2.0, "effective_status": "hit"},
    ]
    assert ledger.effective_status(ops_rows, [], set(), "r1") == "hit"


def test_effective_status_abort_rids_highest_priority():
    ops_rows = [
        {"rid": "r1", "kind": "started", "ts": 1.0},
        {"rid": "r1", "kind": "terminal", "ts": 2.0, "effective_status": "hit"},
    ]
    # 即使 ops 终态是 hit,abort_rids 命中就是最终 error(优先级最高)。
    assert ledger.effective_status(ops_rows, [], {"r1"}, "r1") == "error"


def test_effective_status_response_aborted_overrides_hit():
    ops_rows = [
        {"rid": "r1", "kind": "started", "ts": 1.0},
        {"rid": "r1", "kind": "terminal", "ts": 2.0, "effective_status": "hit"},
    ]
    accepted_events = [{"rid": "r1", "kind": "response_aborted", "ts": 1.5, "reason": "ledger_timeout"}]
    assert ledger.effective_status(ops_rows, accepted_events, set(), "r1") == "error"


def test_effective_status_duplicate_terminal_conflict_is_error():
    ops_rows = [
        {"rid": "r1", "kind": "started", "ts": 1.0},
        {"rid": "r1", "kind": "terminal", "ts": 2.0, "effective_status": "error", "error_code": "ledger_timeout"},
        {"rid": "r1", "kind": "terminal", "ts": 3.0, "effective_status": "hit"},
    ]
    assert ledger.effective_status(ops_rows, [], set(), "r1") == "error"


def test_effective_status_corrupt_terminal_value_is_error():
    ops_rows = [
        {"rid": "r1", "kind": "started", "ts": 1.0},
        {"rid": "r1", "kind": "terminal", "ts": 2.0, "effective_status": "not_a_real_status"},
    ]
    assert ledger.effective_status(ops_rows, [], set(), "r1") == "error"


def test_effective_status_ignores_other_rids():
    ops_rows = [
        {"rid": "r1", "kind": "started", "ts": 1.0},
        {"rid": "r1", "kind": "terminal", "ts": 2.0, "effective_status": "hit"},
        {"rid": "r2", "kind": "started", "ts": 1.0},
    ]
    assert ledger.effective_status(ops_rows, [], set(), "r2") == "error"  # r2 无 terminal


# ======================================================================
# 判别联合:必须缺席字段真缺席,伪值一律 raise
# ======================================================================

def test_discriminated_union_no_fake_fields():
    row = ledger.accepted_row(
        "contract_reject", "r1", 1.0, "real",
        error_code="task_too_long", pre_commit_ms=0.5, config_fp={"a": 1},
    )
    for forbidden_key in ("candidates", "everos_rid", "search_ms", "returned_ids", "q_len"):
        assert forbidden_key not in row
    assert row["query"] is None
    assert row["constructed_decision"] == "error"


def test_discriminated_union_gated_forbidden_and_fixed():
    row = ledger.accepted_row(
        "gated", "r1", 1.0, "real",
        query="已 strip 的查询", q_len=5, pre_commit_ms=1.0, config_fp={},
    )
    assert row["error_code"] == "review_overdue"
    assert row["constructed_decision"] == "error"
    for forbidden_key in ("candidates", "everos_rid", "search_ms", "returned_ids"):
        assert forbidden_key not in row


def test_discriminated_union_forbidden_field_raises_even_with_none():
    with pytest.raises(ValueError):
        ledger.accepted_row(
            "contract_reject", "r1", 1.0, "real",
            error_code="task_empty", pre_commit_ms=0.1, config_fp={},
            candidates=None,  # None 也算"传了值"——必须缺席的字段不接受伪值
        )


def test_discriminated_union_forbidden_field_raises_with_real_value():
    with pytest.raises(ValueError):
        ledger.accepted_row(
            "upstream_fail", "r1", 1.0, "real",
            query="q", q_len=1, error_code="everos_timeout", pre_commit_ms=1.0, config_fp={},
            everos_rid="fake",
        )


def test_discriminated_union_fixed_field_mismatch_raises():
    with pytest.raises(ValueError):
        ledger.accepted_row(
            "gated", "r1", 1.0, "real",
            query="q", q_len=1, pre_commit_ms=1.0, config_fp={},
            error_code="not_review_overdue",
        )


def test_discriminated_union_missing_required_raises():
    with pytest.raises(ValueError):
        ledger.accepted_row("gated", "r1", 1.0, "real")  # 缺 query/q_len/pre_commit_ms/config_fp


def test_discriminated_union_empty_stage_fixed_empty_lists():
    row = ledger.accepted_row(
        "empty", "r1", 1.0, "real",
        query="q", q_len=1, everos_rid="ev1", search_ms=5.0,
        pre_commit_ms=1.0, config_fp={},
    )
    assert row["candidates"] == []
    assert row["returned_ids"] == []
    assert row["constructed_decision"] == "abstain_empty"


def test_discriminated_union_empty_stage_nonempty_candidates_raises():
    with pytest.raises(ValueError):
        ledger.accepted_row(
            "empty", "r1", 1.0, "real",
            query="q", q_len=1, everos_rid="ev1", search_ms=5.0,
            pre_commit_ms=1.0, config_fp={},
            candidates=[{"card_id": "x"}],
        )


def test_discriminated_union_hit_stage_all_fields():
    candidates = [{
        "card_id": "c1", "card_type": "agent_case", "source_rank": 0,
        "native_score": 0.9, "payload_sha": "a" * 64, "passage_sha": "b" * 64,
        "truncated": False,
    }]
    row = ledger.accepted_row(
        "hit", "r1", 1.0, "real",
        query="q", q_len=1, everos_rid="ev1", search_ms=5.0,
        candidates=candidates, returned_ids=["c1"],
        pre_commit_ms=1.0, config_fp={"x": 1},
    )
    assert row["constructed_decision"] == "hit"
    assert row["candidates"] == candidates
    assert "error_code" not in row  # 没传就不出现,不用 None 占位


def test_response_aborted_row_kind_and_fields():
    row = ledger.response_aborted_row("r1", "ledger_timeout")
    assert row["kind"] == "response_aborted"
    assert row["rid"] == "r1"
    assert row["reason"] == "ledger_timeout"


def test_ops_terminal_error_without_error_code_raises():
    with pytest.raises(ValueError):
        ledger.ops_terminal("r1", "error")


def test_ops_terminal_hit_has_no_error_code_key():
    row = ledger.ops_terminal("r1", "hit")
    assert "error_code" not in row


# ======================================================================
# aborts.log:writer 挂死时仍可写
# ======================================================================

def test_mark_abort_works_even_when_writer_wedged(tmp_path, monkeypatch):
    led = ledger.Ledger(tmp_path / "root")
    try:
        real_fsync = os.fsync
        release = threading.Event()

        def blocking_fsync(fd):
            release.wait(timeout=10)
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", blocking_fsync)

        def submit_and_expect_timeout():
            with pytest.raises(ledger.LedgerTimeout):
                led.ops.submit({"rid": "wedged", "kind": "started"}, timeout=0.2)

        wedge_thread = threading.Thread(target=submit_and_expect_timeout)
        wedge_thread.start()
        time.sleep(0.1)

        # ops writer 此刻卡在 blocking_fsync 里出不来,mark_abort 完全不经过
        # 它,必须立刻成功。
        led.mark_abort("r-aborted")

        release.set()
        wedge_thread.join(timeout=10)

        assert ledger.read_abort_rids(led.root) == {"r-aborted"}
    finally:
        # monkeypatch 已在 fixture 结束时自动还原 os.fsync,close() 不会再卡住
        led.close()


def test_mark_abort_file_permissions_and_half_line_tolerance(tmp_path):
    led = ledger.Ledger(tmp_path / "root")
    try:
        led.mark_abort("r1")
        aborts_path = led.root / "aborts.log"
        assert stat.S_IMODE(aborts_path.stat().st_mode) == 0o600
        with open(aborts_path, "a", encoding="utf-8") as f:
            f.write('{"rid": "half-lin')  # 手写半行,读取端应跳过不炸
        assert ledger.read_abort_rids(led.root) == {"r1"}
    finally:
        led.close()


# ======================================================================
# scored-writer:attempt_no 串行分配 + 恢复,validator 改写
# ======================================================================

def test_scored_attempt_no_serial_and_recovered_after_restart(tmp_path):
    root = tmp_path / "root"
    led1 = ledger.Ledger(root)
    row = ledger.scored_row("r1", "realtime", "ok", {}, {})
    led1.submit_scored(row, {"rid": "r1"}, timeout=5.0)
    led1.submit_scored(ledger.scored_row("r1", "realtime", "ok", {}, {}), {"rid": "r1"}, timeout=5.0)
    led1.close()

    rows, _ = ledger.iter_rows(root, "scored")
    assert [r["attempt_no"] for r in rows] == [0, 1]

    led2 = ledger.Ledger(root)
    try:
        led2.submit_scored(ledger.scored_row("r1", "realtime", "ok", {}, {}), {"rid": "r1"}, timeout=5.0)
    finally:
        led2.close()

    rows, _ = ledger.iter_rows(root, "scored")
    assert [r["attempt_no"] for r in rows] == [0, 1, 2]  # 跨重启单调,不重号


def test_scored_attempt_no_independent_per_rid(tmp_path):
    led = ledger.Ledger(tmp_path / "root")
    try:
        led.submit_scored(ledger.scored_row("r1", "realtime", "ok", {}, {}), {"rid": "r1"}, timeout=5.0)
        led.submit_scored(ledger.scored_row("r2", "realtime", "ok", {}, {}), {"rid": "r2"}, timeout=5.0)
        led.submit_scored(ledger.scored_row("r1", "realtime", "ok", {}, {}), {"rid": "r1"}, timeout=5.0)
    finally:
        led.close()
    rows, _ = ledger.iter_rows(led.root, "scored")
    by_rid = {}
    for r in rows:
        by_rid.setdefault(r["rid"], []).append(r["attempt_no"])
    assert by_rid["r1"] == [0, 1]
    assert by_rid["r2"] == [0]


def test_scored_validator_rewrites_unhealthy_ok_row(tmp_path):
    def unhealthy(row, accepted_row):
        return False

    led = ledger.Ledger(tmp_path / "root", scored_validator=unhealthy)
    try:
        row = ledger.scored_row("r1", "realtime", "ok", {"agent_case:c1": {}}, {"embed_model": "x"})
        led.submit_scored(row, {"rid": "r1", "candidates": []}, timeout=5.0)
    finally:
        led.close()

    rows, _ = ledger.iter_rows(led.root, "scored")
    assert len(rows) == 1
    assert rows[0]["status"] == "retryable_error"
    assert rows[0]["score_error_code"] == "health_predicate_reject"


def test_scored_validator_not_called_for_non_ok_rows(tmp_path):
    calls = []

    def validator(row, accepted_row):
        calls.append(row["status"])
        return True

    led = ledger.Ledger(tmp_path / "root", scored_validator=validator)
    try:
        row = ledger.scored_row("r1", "realtime", "retryable_error", {}, {}, score_error_code="internal")
        led.submit_scored(row, {"rid": "r1"}, timeout=5.0)
    finally:
        led.close()

    assert calls == []  # validator 只对 status=="ok" 的行调用


def test_scored_validator_receives_accepted_row_argument(tmp_path):
    received = {}

    def validator(row, accepted_row):
        received["accepted_row"] = accepted_row
        return True

    led = ledger.Ledger(tmp_path / "root", scored_validator=validator)
    accepted = {"rid": "r1", "candidates": [{"card_id": "c1"}]}
    try:
        row = ledger.scored_row("r1", "realtime", "ok", {"agent_case:c1": {}}, {"embed_model": "x"})
        led.submit_scored(row, accepted, timeout=5.0)
    finally:
        led.close()

    assert received["accepted_row"] is accepted


def test_scored_row_no_attempt_no_or_written_ts_before_writer():
    row = ledger.scored_row("r1", "realtime", "ok", {}, {})
    assert "attempt_no" not in row
    assert "written_ts" not in row


# ======================================================================
# 权限:root 0700,四个账文件 0600
# ======================================================================

def test_permissions_root_dir_0700_and_files_0600(tmp_path):
    led = ledger.Ledger(tmp_path / "root")
    try:
        assert stat.S_IMODE(led.root.stat().st_mode) == 0o700
        for name in ("ops.jsonl", "accepted.jsonl", "scored.jsonl", "aborts.log", ".lock"):
            path = led.root / name
            assert path.exists(), name
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
    finally:
        led.close()


def test_preexisting_root_dir_wrong_mode_raises_not_repaired(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o755)  # 故意摆错——模拟"已存在但权限不对"的账目录

    with pytest.raises(ledger.LedgerPermissionError):
        ledger.Ledger(root)

    # 拒绝启动,而且绝不「顺手」把这个错误权限修好——校验之后现场权限原样保留。
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_preexisting_stream_file_wrong_mode_raises_not_repaired(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    accepted_path = root / "accepted.jsonl"
    accepted_path.write_text(json.dumps({"rid": "r1"}) + "\n", encoding="utf-8")
    os.chmod(accepted_path, 0o644)  # 故意摆错,且不是残尾(结尾有 \n),不会被重新封段

    with pytest.raises(ledger.LedgerPermissionError):
        ledger.Ledger(root)

    assert stat.S_IMODE(accepted_path.stat().st_mode) == 0o644  # 没被静默修复


def test_preexisting_lock_file_wrong_mode_raises_not_repaired(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    lock_path = root / ".lock"
    lock_path.write_bytes(b"")
    os.chmod(lock_path, 0o644)  # 故意摆错

    with pytest.raises(ledger.LedgerPermissionError):
        ledger.Ledger(root)

    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o644


def test_permission_failure_after_lock_acquired_releases_lock(tmp_path):
    """权限校验在拿到 flock 之后才跑(启动协议①在②③之前)——校验失败必须把
    锁放掉,否则调用方修好权限后重开同一个 root 会被自己这次失败的残留 flock
    误挡成 LedgerLocked(这是实现 verify-then-raise 时必须连带修的一个真实
    资源泄漏点,而不是这条测试本身要求的行为)。"""
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o755)  # 故意摆错,触发 _verify_permissions 失败

    with pytest.raises(ledger.LedgerPermissionError):
        ledger.Ledger(root)

    os.chmod(root, 0o700)  # 人工修好
    led = ledger.Ledger(root)  # 如果锁泄漏了,这里会变成 LedgerLocked
    led.close()


def test_correct_preexisting_permissions_start_fine(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    lock_path = root / ".lock"
    lock_path.write_bytes(b"")
    os.chmod(lock_path, 0o600)
    accepted_path = root / "accepted.jsonl"
    accepted_path.write_text(json.dumps({"rid": "r1"}) + "\n", encoding="utf-8")
    os.chmod(accepted_path, 0o600)

    led = ledger.Ledger(root)  # 权限全部合规——应该正常启动,不 raise
    try:
        rows, _ = ledger.iter_rows(root, "accepted")
        assert rows == [{"rid": "r1"}]
    finally:
        led.close()


# ======================================================================
# alive() / close()
# ======================================================================

def test_writer_alive_then_dead_after_close(tmp_path):
    writer = ledger.LedgerWriter(tmp_path / "ops.jsonl", "ops")
    assert writer.alive()
    writer.close(drain=True)
    assert not writer.alive()


def test_ops_write_fail_fault_on_ledger(tmp_path):
    led = ledger.Ledger(tmp_path / "root", fault="ops_write_fail")
    try:
        with pytest.raises(ledger.LedgerUnavailable):
            led.ops.submit({"rid": "r1", "kind": "started"}, timeout=5.0)
        # accepted 流不受影响(故障只注入到 ops writer)
        led.accepted.submit({"rid": "r1", "kind": "accepted"}, timeout=5.0)
    finally:
        led.close()
    rows, _ = ledger.iter_rows(led.root, "accepted")
    assert len(rows) == 1


def test_accepted_write_fail_fault_on_ledger(tmp_path):
    led = ledger.Ledger(tmp_path / "root", fault="accepted_write_fail")
    try:
        with pytest.raises(ledger.LedgerUnavailable):
            led.accepted.submit({"rid": "r1", "kind": "accepted"}, timeout=5.0)
        # ops/scored 流不受影响(故障只注入到 accepted writer)
        led.ops.submit({"rid": "r1", "kind": "started"}, timeout=5.0)
    finally:
        led.close()
    rows, _ = ledger.iter_rows(led.root, "ops")
    assert len(rows) == 1
    accepted_rows, _ = ledger.iter_rows(led.root, "accepted")
    assert accepted_rows == []


def test_close_drain_false_still_releases_flock(tmp_path):
    root = tmp_path / "root"
    led1 = ledger.Ledger(root)
    led1.close(drain=False)
    # 就算不等排空,flock 也必须放掉——否则下一个实例会被误挡。
    led2 = ledger.Ledger(root)
    led2.close()
