"""
tests/test_m3_backup.py — §11.3 bridge state 备份 restore smoke 测试。
不依赖任何 live service；只验 sqlite .backup 往返 + 计数一致性。
"""
import subprocess
import os
import pytest
from pathlib import Path
from distill import state


def test_bridge_state_restore_smoke(tmp_path):
    """sqlite .backup → restore → cursor + raw_work_item 计数一致"""
    src = str(tmp_path / "bridge.db")
    dst = str(tmp_path / "restored.db")

    # 建立状态库并写入测试数据
    c = state.connect(src)
    c.execute("INSERT INTO cursor(source_id, stream_position) VALUES('ubuntu-cc', 42)")
    c.execute(
        "INSERT INTO raw_work_item(source_id, conversation_id, span_start, span_end, session_ref, status, created_at)"
        " VALUES('ubuntu-cc', 1, 1, 9, 's', 'distilled', '2026-06-24')"
    )
    c.commit()
    c.close()

    # 脚本路径以项目根为基准（pytest 从项目根跑）
    script = "infra/backup/restore-bridge-smoke.sh"
    rc = subprocess.run(
        ["bash", script, src, dst],
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0, f"restore-bridge-smoke.sh failed:\n{rc.stderr}"

    # 验 restore 结果
    r = state.connect(dst)
    pos = r.execute(
        "SELECT stream_position FROM cursor WHERE source_id='ubuntu-cc'"
    ).fetchone()
    assert pos is not None, "cursor row 未还原"
    assert pos[0] == 42, f"stream_position 期望 42，得 {pos[0]}"

    cnt = r.execute("SELECT COUNT(*) FROM raw_work_item").fetchone()[0]
    assert cnt == 1, f"raw_work_item 期望 1 行，得 {cnt}"
    r.close()


def test_bridge_state_restore_smoke_stdout(tmp_path):
    """restore-bridge-smoke.sh 成功时 stdout 含 'restore-bridge-smoke OK'"""
    src = str(tmp_path / "bridge.db")
    dst = str(tmp_path / "restored.db")

    c = state.connect(src)
    c.commit()
    c.close()

    rc = subprocess.run(
        ["bash", "infra/backup/restore-bridge-smoke.sh", src, dst],
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0, rc.stderr
    assert "restore-bridge-smoke OK" in rc.stdout


def test_bridge_state_restore_preserves_journal(tmp_path):
    """journal 表（pending/done idempotency keys）也须在 restore 后完整"""
    src = str(tmp_path / "bridge.db")
    dst = str(tmp_path / "restored.db")

    c = state.connect(src)
    # 先插 raw_work_item（journal 有 FK）
    c.execute(
        "INSERT INTO raw_work_item(source_id, conversation_id, span_start, span_end, session_ref, status, created_at)"
        " VALUES('ubuntu-cc', 10, 1, 5, 'ref', 'distilled', '2026-06-24')"
    )
    c.commit()
    raw_id = c.execute("SELECT id FROM raw_work_item LIMIT 1").fetchone()[0]
    c.execute(
        "INSERT INTO journal(key, raw_work_item_id, entity_slug, entry_type, fact_text, source_ref, entry_date, status, created_at)"
        " VALUES('k1', ?, 'projects/test', 'fact', 'test fact', 'conv:10', '2026-06-24', 'done', '2026-06-24T00:00:00Z')",
        (raw_id,),
    )
    c.commit()
    c.close()

    rc = subprocess.run(
        ["bash", "infra/backup/restore-bridge-smoke.sh", src, dst],
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0, rc.stderr

    r = state.connect(dst)
    jcnt = r.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    assert jcnt == 1, f"journal 期望 1 行，得 {jcnt}"
    r.close()


def test_restore_smoke_missing_src(tmp_path):
    """源库不存在时脚本应以非零退出"""
    src = str(tmp_path / "nonexistent.db")
    dst = str(tmp_path / "out.db")
    rc = subprocess.run(
        ["bash", "infra/backup/restore-bridge-smoke.sh", src, dst],
        capture_output=True,
        text=True,
    )
    assert rc.returncode != 0, "不存在的源库应导致脚本失败"
