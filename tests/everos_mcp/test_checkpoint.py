"""checkpoint.py 的测试(P4 Task 5:有界运行机制)。

固定纪律(见 everos_mcp/checkpoint.py 顶部文档字符串 + 任务简报):
- 无 meta 且无账行 -> 原子创建;有账行但 meta 缺失/损坏/launched_ts 晚于
  最早账行 ts -> fail-closed 拒启(`CheckpointCorrupt`)。
- 到点判据:`now-launched_ts >= 30 天` 或 `real_query_count >= 200`;首次
  判到点时原子持久化 `due_since`。到点 7 天无复审 -> "overdue"。
- 复审必须发生在 `due_since` **之后**才能把状态解回 "ok"——早于 due_since
  的复审(到点前的自愿复审)不算数(任务给定口径)。
- 全部用注入 `now`,不依赖 wall-clock sleep;fixture 全合成,零拓扑字面量。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest

from everos_mcp import checkpoint

_DAY = 86400
_T0 = 1_700_000_000.0  # 合成基准时间戳(与任何真实上线时间无关)


# ======================================================================
# init_or_load:fail-closed 三态 + 正常创建/加载
# ======================================================================

def test_init_or_load_creates_new_when_no_meta_no_ledger_rows(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    meta = cp.init_or_load(ledger_has_rows=False, now=_T0)
    assert meta["launched_ts"] == _T0
    assert meta["due_since"] is None
    assert meta["reviews"] == []
    assert cp.meta_path.exists()


def test_init_or_load_meta_file_permission_0600(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    mode = stat.S_IMODE(cp.meta_path.stat().st_mode)
    assert mode == 0o600


def test_init_or_load_no_leftover_tmp_files(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    leftovers = list((tmp_path / "root").glob(".tmp.*"))
    assert leftovers == []


def test_init_or_load_loads_existing_meta_unchanged(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    first = cp.init_or_load(ledger_has_rows=False, now=_T0)
    # 第二次调用(模拟重启):有账行、launched_ts 早于最早账行 ts -> 正常加载,
    # 不重建、不改动 launched_ts。
    second = cp.init_or_load(ledger_has_rows=True, earliest_ledger_ts=_T0 + 10, now=_T0 + 100)
    assert second["launched_ts"] == first["launched_ts"] == _T0


def test_init_or_load_refuses_when_ledger_has_rows_but_no_meta(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=True, earliest_ledger_ts=_T0, now=_T0)


def test_init_or_load_refuses_on_corrupt_json(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text("{not valid json", encoding="utf-8")
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=True, earliest_ledger_ts=_T0, now=_T0)


def test_init_or_load_refuses_on_meta_missing_launched_ts_field(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(json.dumps({"due_since": None, "reviews": []}), encoding="utf-8")
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=False, now=_T0)


def test_init_or_load_requires_earliest_ts_when_ledger_has_rows_and_meta_exists(tmp_path):
    """meta 已存在、ledger_has_rows=True 但调用方没给 earliest_ledger_ts——这会
    让回拨校验被静默跳过,必须 fail loud(ValueError)而不是悄悄放行。"""
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    with pytest.raises(ValueError):
        cp.init_or_load(ledger_has_rows=True, earliest_ledger_ts=None, now=_T0 + 100)


def test_init_or_load_refuses_on_launched_ts_rollback(tmp_path):
    """launched_ts 晚于最早账行 ts = 时钟回拨或 meta 被换过——拒启。"""
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        # 最早账行 ts 比 launched_ts 还早 -> 不合理(账本比 checkpoint 启动还老)
        cp.init_or_load(ledger_has_rows=True, earliest_ledger_ts=_T0 - 10, now=_T0 + 5)


# ======================================================================
# state():due/overdue 时序(注入 now)
# ======================================================================

def test_state_ok_before_due(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    assert cp.state(real_query_count=5, now=_T0 + _DAY) == "ok"


def test_state_due_by_time_threshold(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    assert cp.state(real_query_count=0, now=_T0 + 30 * _DAY) == "due"


def test_state_due_by_query_count_threshold(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    assert cp.state(real_query_count=200, now=_T0 + _DAY) == "due"


def test_state_not_due_just_below_thresholds(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    assert cp.state(real_query_count=199, now=_T0 + 30 * _DAY - 1) == "ok"


def test_state_persists_due_since_on_first_due_judgment(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    due_ts = _T0 + 30 * _DAY
    cp.state(real_query_count=0, now=due_ts)
    on_disk = json.loads(cp.meta_path.read_text(encoding="utf-8"))
    assert on_disk["due_since"] == due_ts


def test_state_due_since_does_not_move_on_repeated_judgment(tmp_path):
    """重复判定到点(比如 watchdog 每 60s 轮询一次)不应该改写已经持久化的
    due_since——否则 7 天宽限期会被每次轮询无限往后推。"""
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    due_ts = _T0 + 30 * _DAY
    cp.state(real_query_count=0, now=due_ts)
    cp.state(real_query_count=0, now=due_ts + 60)
    on_disk = json.loads(cp.meta_path.read_text(encoding="utf-8"))
    assert on_disk["due_since"] == due_ts


def test_state_stays_due_within_grace_period(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    due_ts = _T0 + 30 * _DAY
    cp.state(real_query_count=0, now=due_ts)
    assert cp.state(real_query_count=0, now=due_ts + 6 * _DAY) == "due"


def test_state_overdue_after_grace_period_with_no_review(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    due_ts = _T0 + 30 * _DAY
    cp.state(real_query_count=0, now=due_ts)
    assert cp.state(real_query_count=0, now=due_ts + 7 * _DAY) == "overdue"


# ======================================================================
# record_review():回 ok + 时序口径(早于 due_since 的复审不算数)
# ======================================================================

def test_record_review_after_due_since_returns_state_to_ok(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    due_ts = _T0 + 30 * _DAY
    cp.state(real_query_count=0, now=due_ts)
    assert cp.state(real_query_count=0, now=due_ts + 7 * _DAY) == "overdue"

    cp.record_review(decision="continue", by="ivan", note="reviewed after overdue", now=due_ts + 8 * _DAY)
    assert cp.state(real_query_count=0, now=due_ts + 8 * _DAY) == "ok"
    # 复审之后恒 ok(到点判据单调只增,due_since 不会重置,复审 ts>=due_since 恒成立)
    assert cp.state(real_query_count=0, now=due_ts + 30 * _DAY) == "ok"


def test_record_review_before_due_since_does_not_clear_later_due(tmp_path):
    """到点之前的自愿复审(ts < due_since)不能解除之后才出现的到点/逾期。"""
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    # 早期自愿复审,此时甚至还没到点
    cp.record_review(decision="continue", by="ivan", note="early voluntary review", now=_T0 + 1)

    due_ts = _T0 + 30 * _DAY
    assert cp.state(real_query_count=0, now=due_ts) == "due"
    assert cp.state(real_query_count=0, now=due_ts + 7 * _DAY) == "overdue"


def test_record_review_rejects_invalid_decision(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    with pytest.raises(ValueError):
        cp.record_review(decision="not-a-real-decision", by="ivan", note="x", now=_T0 + 1)


def test_record_review_appends_ts_decision_by_note(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    cp.record_review(decision="calibrate", by="ivan", note="校准阈值", now=_T0 + 1)
    on_disk = json.loads(cp.meta_path.read_text(encoding="utf-8"))
    assert on_disk["reviews"] == [
        {"ts": _T0 + 1, "decision": "calibrate", "by": "ivan", "note": "校准阈值"}
    ]


def test_record_review_before_init_or_load_raises_corrupt(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.record_review(decision="continue", by="ivan", note="x", now=_T0)


def test_state_before_init_or_load_raises_corrupt(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.state(real_query_count=0, now=_T0)


# ======================================================================
# CLI:python -m everos_mcp.checkpoint review --decision ... --by ... --note ...
# ======================================================================

def _cli_env(ledger_dir):
    env = dict(os.environ)
    env.update(
        {
            "EVEROS_MCP_PORT": "1",
            "EVEROS_MCP_TOKEN": "test-token",
            "EVEROS_BASE_URL": "http://127.0.0.1:1",
            "EVEROS_AGENT_ID": "test-agent",
            "INFINITY_BASE": "http://127.0.0.1:1",
            "SHADOW_LEDGER_DIR": str(ledger_dir),
            "EVEROS_EMBED_MODEL": "test-embed-model",
            "EVEROS_RERANK_MODEL": "test-rerank-model",
            "EVEROS_PIN_FILE": str(ledger_dir / "pin.json"),
            "EVEROS_INSTANCE_DIR": str(ledger_dir / "instance"),
            "INFINITY_CONTAINER": "test-container",
        }
    )
    return env


def test_cli_review_records_review_via_config_ledger_dir(tmp_path):
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    # CLI 路径下 root == config.load().ledger_dir,先手工建好 meta(模拟 server 已跑过)
    cp = checkpoint.Checkpoint(ledger_dir)
    cp.init_or_load(ledger_has_rows=False, now=_T0)

    result = subprocess.run(
        [
            sys.executable, "-m", "everos_mcp.checkpoint", "review",
            "--decision", "continue", "--by", "ivan", "--note", "cli 冒烟测试",
        ],
        env=_cli_env(ledger_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    on_disk = json.loads((ledger_dir / "meta.json").read_text(encoding="utf-8"))
    assert len(on_disk["reviews"]) == 1
    entry = on_disk["reviews"][0]
    assert entry["decision"] == "continue"
    assert entry["by"] == "ivan"
    assert entry["note"] == "cli 冒烟测试"


def test_cli_review_rejects_invalid_decision(tmp_path):
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    result = subprocess.run(
        [
            sys.executable, "-m", "everos_mcp.checkpoint", "review",
            "--decision", "bogus", "--by", "ivan", "--note", "x",
        ],
        env=_cli_env(ledger_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
