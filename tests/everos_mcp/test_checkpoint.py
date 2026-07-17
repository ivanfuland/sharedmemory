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

import fcntl
import json
import os
import stat
import subprocess
import sys
import threading
import time

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


def test_init_or_load_refuses_on_launched_ts_nan(tmp_path):
    """`json.loads` 默认接受 `NaN` 字面量,`isinstance(NaN, float)` 为真——单纯
    类型检查挡不住;必须显式 `math.isfinite` 校验(P1g)。"""
    root = tmp_path / "root"
    root.mkdir()
    # json.dumps(float('nan')) 写出裸 `NaN` 字面量(Python json 模块的非标准扩展)
    # ——这正是本测试要覆盖的绕过路径:`json.loads` 照单全收,`isinstance` 也认
    # 它是 float,唯有 `math.isfinite` 能拦。
    (root / "meta.json").write_text(
        json.dumps({"launched_ts": float("nan"), "due_since": None, "reviews": []}),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=False, now=_T0)


def test_init_or_load_refuses_on_launched_ts_infinity(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({"launched_ts": float("inf"), "due_since": None, "reviews": []}),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=False, now=_T0)


def test_state_refuses_on_due_since_nan(tmp_path):
    """due_since=NaN 落盘(如损坏/篡改)-> state() 读取时必须 fail-closed,不能
    让后续 `now - due_since` 静默算出 NaN 再让比较运算悄悄永远为 False。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({"launched_ts": _T0, "due_since": float("nan"), "reviews": []}),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.state(real_query_count=0, now=_T0 + 10)


@pytest.mark.parametrize("bad_reviews", [{}, "", 0, False])
def test_init_or_load_refuses_on_reviews_falsy_non_list(tmp_path, bad_reviews):
    """R4 阻断项 #5:`obj.get("reviews") or []` 对 falsy 但非列表的值
    (`{}`/`""`/`0`/`False`)会被静默当成"没有复审记录",篡改/损坏因此被悄悄
    放行而不是 fail-closed。`reviews` 键存在但不是列表 -> 必须
    `CheckpointCorrupt`,不论这个值真假。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({"launched_ts": _T0, "due_since": None, "reviews": bad_reviews}),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=False, now=_T0)


def test_init_or_load_refuses_on_reviews_truthy_non_list(tmp_path):
    """真值但非列表(如非空字符串)同样必须 fail-closed——不是只挡 falsy 值。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({"launched_ts": _T0, "due_since": None, "reviews": "not-a-list"}),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=False, now=_T0)


def test_state_refuses_on_review_ts_nan(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({
            "launched_ts": _T0, "due_since": _T0 + 5,
            "reviews": [{"ts": float("nan"), "decision": "continue", "by": "x", "note": "n"}],
        }),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.state(real_query_count=0, now=_T0 + 10)


@pytest.mark.parametrize(
    "bad_review",
    [
        {"ts": _T0 + 1},  # 裸 ts,缺 decision/by/note
        {"ts": _T0 + 1, "decision": "continue"},  # 缺 by/note
        {"ts": _T0 + 1, "decision": "continue", "by": "x"},  # 缺 note
    ],
)
def test_init_or_load_refuses_on_review_missing_required_fields(tmp_path, bad_review):
    """复审记录是豁免-补偿的审计轨迹,`{"ts": due_since}` 这类裸字段绝不能
    解锁 overdue——必须同时具备 ts/decision/by/note 四个字段,缺一律
    `CheckpointCorrupt`(fail-closed,不静默放行"代拍")。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({"launched_ts": _T0, "due_since": _T0 + 5, "reviews": [bad_review]}),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=False, now=_T0)


def test_init_or_load_refuses_on_review_bogus_decision(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({
            "launched_ts": _T0, "due_since": _T0 + 5,
            "reviews": [{"ts": _T0 + 6, "decision": "bogus", "by": "reviewer-1", "note": "n"}],
        }),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=False, now=_T0)


def test_init_or_load_refuses_on_review_empty_by(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({
            "launched_ts": _T0, "due_since": _T0 + 5,
            "reviews": [{"ts": _T0 + 6, "decision": "continue", "by": "", "note": "n"}],
        }),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=False, now=_T0)


def test_init_or_load_refuses_on_review_non_str_note(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({
            "launched_ts": _T0, "due_since": _T0 + 5,
            "reviews": [{"ts": _T0 + 6, "decision": "continue", "by": "reviewer-1", "note": 123}],
        }),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=False, now=_T0)


def test_init_or_load_refuses_on_due_since_before_launched_ts(tmp_path):
    """`due_since` 早于 `launched_ts` 不可能合法出现(到点判据只在启动之后
    才会成立)——多半是篡改/损坏,fail-closed 拒绝加载。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({"launched_ts": _T0, "due_since": _T0 - 1, "reviews": []}),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.init_or_load(ledger_has_rows=False, now=_T0)


def test_init_or_load_accepts_full_schema_valid_review(tmp_path):
    """全字段合法的复审记录必须照旧正常加载并按既有语义解除到点——本修复
    只收紧非法输入,不改变合法输入的行为。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({
            "launched_ts": _T0, "due_since": _T0 + 5,
            "reviews": [{"ts": _T0 + 6, "decision": "continue", "by": "reviewer-1", "note": "ok"}],
        }),
        encoding="utf-8",
    )
    cp = checkpoint.Checkpoint(root)
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    assert cp.state(real_query_count=0, now=_T0 + 10) == "ok"


def test_record_review_rejects_empty_by(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    with pytest.raises(ValueError):
        cp.record_review(decision="continue", by="", note="x", now=_T0 + 1)
    with pytest.raises(ValueError):
        cp.record_review(decision="continue", by="   ", note="x", now=_T0 + 1)


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


def test_state_due_since_is_sticky_despite_lower_later_query_count(tmp_path):
    """一旦 due_since 落盘(比如计数触发到点),之后某次调用即便传入更低的
    real_query_count(计数上报非单调 / 重启抖动),状态也不能被静默原谅回
    "ok"——必须仍是 due,且到点后的逾期推进(overdue)照旧从原 due_since
    起算。这是回归测试(此前实现里 state() 每次都用当次入参重算 is_due,
    没先看持久化的 due_since,导致这个场景被误判成 ok)。"""
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)

    due_ts = _T0 + _DAY
    assert cp.state(real_query_count=200, now=due_ts) == "due"
    on_disk = json.loads(cp.meta_path.read_text(encoding="utf-8"))
    assert on_disk["due_since"] == due_ts

    # 之后一次调用:count 掉回 5(远低于 200),时间也没到 30 天——若只看当次
    # is_due 会误判 ok;必须仍是 due,因为 due_since 已经落盘且无复审。
    assert cp.state(real_query_count=5, now=due_ts + _DAY) == "due"

    # 逾期推进照旧从原 due_since 起算,不受这次低计数调用影响。
    assert cp.state(real_query_count=5, now=due_ts + 7 * _DAY) == "overdue"


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

    cp.record_review(decision="continue", by="reviewer-1", note="reviewed after overdue", now=due_ts + 8 * _DAY)
    assert cp.state(real_query_count=0, now=due_ts + 8 * _DAY) == "ok"
    # 复审之后恒 ok(到点判据单调只增,due_since 不会重置,复审 ts>=due_since 恒成立)
    assert cp.state(real_query_count=0, now=due_ts + 30 * _DAY) == "ok"


def test_record_review_before_due_since_does_not_clear_later_due(tmp_path):
    """到点之前的自愿复审(ts < due_since)不能解除之后才出现的到点/逾期。"""
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    # 早期自愿复审,此时甚至还没到点
    cp.record_review(decision="continue", by="reviewer-1", note="early voluntary review", now=_T0 + 1)

    due_ts = _T0 + 30 * _DAY
    assert cp.state(real_query_count=0, now=due_ts) == "due"
    assert cp.state(real_query_count=0, now=due_ts + 7 * _DAY) == "overdue"


def test_record_review_rejects_invalid_decision(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    with pytest.raises(ValueError):
        cp.record_review(decision="not-a-real-decision", by="reviewer-1", note="x", now=_T0 + 1)


def test_record_review_appends_ts_decision_by_note(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    cp.record_review(decision="calibrate", by="reviewer-1", note="校准阈值", now=_T0 + 1)
    on_disk = json.loads(cp.meta_path.read_text(encoding="utf-8"))
    assert on_disk["reviews"] == [
        {"ts": _T0 + 1, "decision": "calibrate", "by": "reviewer-1", "note": "校准阈值"}
    ]


def test_record_review_before_init_or_load_raises_corrupt(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    with pytest.raises(checkpoint.CheckpointCorrupt):
        cp.record_review(decision="continue", by="reviewer-1", note="x", now=_T0)


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
            "--decision", "continue", "--by", "reviewer-1", "--note", "cli 冒烟测试",
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
    assert entry["by"] == "reviewer-1"
    assert entry["note"] == "cli 冒烟测试"


def test_cli_review_rejects_invalid_decision(tmp_path):
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    result = subprocess.run(
        [
            sys.executable, "-m", "everos_mcp.checkpoint", "review",
            "--decision", "bogus", "--by", "reviewer-1", "--note", "x",
        ],
        env=_cli_env(ledger_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0


# ======================================================================
# 跨进程锁:meta.lock flock(final-review 修复项,extends M5.x)
#
# 真正的双进程竞态难在单测里稳定复现;这里用"两个各自独立实例化的
# Checkpoint、跑在两条线程里"作为最小可行证据——`Checkpoint` 本身没有任何
# 进程内锁(server.py 的 `_CHECKPOINT_LOCK` 是调用方自己加的,不在本类里),
# 所以哪怕是同进程的两个线程并发调用同一 root 上的两个独立 Checkpoint 实例,
# 命中的竞态窗口与两个真实进程完全一致——串行化完全来自本模块新增的
# `root/meta.lock` flock,不是靠 GIL 或任何 Python 级别的锁蒙混过关。
# ======================================================================

def test_meta_lock_serializes_concurrent_state_and_record_review_across_instances(tmp_path):
    """两个独立 `Checkpoint` 实例(模拟 server 进程 vs CLI 进程)并发触发
    `state()` 的首次 due_since 持久化与 `record_review()` 的追加——若读-改-写
    没有互斥,先读到的一方会用自己内存里的旧 meta 覆盖掉另一方已经落盘的字段
    (丢更新)。用 `_atomic_write_json` 打个可控延迟撑大竞态窗口,断言两次写
    的结果最终都完整可见(不管谁先拿到锁)。"""
    root = tmp_path / "root"
    seed = checkpoint.Checkpoint(root)
    seed.init_or_load(ledger_has_rows=False, now=_T0)

    due_ts = _T0 + 30 * _DAY
    barrier = threading.Barrier(2)
    orig_atomic_write = checkpoint._atomic_write_json

    def _slow_atomic_write(path, obj):
        # 在真正落盘前人为撑开一个窗口,放大"两边都基于旧快照改"的竞态概率
        time.sleep(0.05)
        orig_atomic_write(path, obj)

    def _state_worker():
        cp = checkpoint.Checkpoint(root)  # 独立实例,模拟另一个进程
        barrier.wait()
        cp.state(real_query_count=0, now=due_ts)

    def _review_worker():
        cp = checkpoint.Checkpoint(root)  # 独立实例,模拟 CLI 进程
        barrier.wait()
        cp.record_review(decision="continue", by="race-probe", note="并发探针", now=due_ts)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(checkpoint, "_atomic_write_json", _slow_atomic_write)
        t1 = threading.Thread(target=_state_worker)
        t2 = threading.Thread(target=_review_worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive() and not t2.is_alive()

    on_disk = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    # 不管谁先拿到 flock,两次写入都必须完整体现——没有一方的更新被另一方
    # 基于旧快照的写覆盖掉。
    assert on_disk["due_since"] == due_ts
    assert len(on_disk["reviews"]) == 1
    assert on_disk["reviews"][0]["by"] == "race-probe"


def test_meta_lock_file_permission_0600(tmp_path):
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    mode = stat.S_IMODE(cp.lock_path.stat().st_mode)
    assert mode == 0o600


def test_meta_lock_released_after_each_operation(tmp_path):
    """每次操作后锁必须释放——用一个独立 fd 以非阻塞 flock 去抢同一把锁,
    只要不抛异常就证明上一次操作没有把锁遗留在持有状态。"""
    cp = checkpoint.Checkpoint(tmp_path / "root")
    cp.init_or_load(ledger_has_rows=False, now=_T0)
    cp.state(real_query_count=0, now=_T0 + 1)
    cp.record_review(decision="continue", by="reviewer-1", note="x", now=_T0 + 2)

    probe_fd = os.open(cp.lock_path, os.O_RDWR)
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 拿不到就会抛 OSError
        fcntl.flock(probe_fd, fcntl.LOCK_UN)
    finally:
        os.close(probe_fd)
