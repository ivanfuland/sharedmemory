"""infra/backup/cass/cass_backup_gate.py 的 rebaseline CLI 测试（Task 7 Step 2，
spec §5.7 / §9.1 V5g2 / V5g2b / V5g2c / V15g）。

覆盖：
  - V5g2b：`--rebaseline`/`--rebaseline-reason` 成对性校验——缺一即 exit 2（CLI
    双保险；bash 层的成对校验属于 Task 9）。
  - V5g2c/V15g：rebaseline 目标三项校验——① 目录不存在、② 缺 `COMPLETE`、
    ③ 不是链 tip（`touch` 让非 tip 目录 mtime 最新仍必须拒绝，只认 `generation`）
    ——三种都必须 exit 2。这三个测试不需要真的跑五腿门（目标校验在开 db 之前就
    拒绝），所以不依赖 `synth_dd`/真 `cass` 二进制，`--db` 给个不存在的路径即可。
  - V5g2：rebaseline 不是 bypass-all——目标校验通过后，用攻击①跑仍必须 FAIL
    （必需对象清单不受 rebaseline 影响）；用「合法迁移」夹具（`ALTER TABLE
    ADD COLUMN` 等价物）跑必须 PASS 且 `gate.json` 含 `rebaselined_from`/`reason`
    留痕（键名逐字照抄 spec §5.7/§8.3-C2：就叫 `reason`，不是 `rebaseline_reason`）。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess

import pytest

import cass_common
import fixture_factory

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
GATE_SCRIPT = REPO / "infra" / "backup" / "cass" / "cass_backup_gate.py"

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd 模板"
)


def _run_cli(
    db, dest, out_census, out_gate_json, rebaseline=None, rebaseline_reason=None
) -> tuple[int, str, str]:
    cmd = [
        str(VENV_PY),
        str(GATE_SCRIPT),
        "--db",
        str(db),
        "--dest",
        str(dest),
        "--out-census",
        str(out_census),
        "--out-gate-json",
        str(out_gate_json),
    ]
    if rebaseline is not None:
        cmd += ["--rebaseline", rebaseline]
    if rebaseline_reason is not None:
        cmd += ["--rebaseline-reason", rebaseline_reason]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.returncode, result.stdout, result.stderr


def _publish_baseline(dest, name, gate_json_path, census_path, generation) -> pathlib.Path:
    backup_dir = pathlib.Path(dest) / name
    backup_dir.mkdir(parents=True)
    shutil.copy(census_path, backup_dir / "census.tsv")
    gate = json.loads(pathlib.Path(gate_json_path).read_bytes())
    gate["generation"] = generation
    (backup_dir / "digest.json").write_bytes(cass_common.dumps_canonical(gate))
    (backup_dir / "COMPLETE").touch()
    return backup_dir


def _make_bare_published_dir(dest, name, generation, with_complete=True) -> pathlib.Path:
    """只造 rebaseline 目标校验需要的最小形状：`digest.json`（含 `generation`）+
    可选 `COMPLETE`。不需要真实 census/tables 内容——这三个测试在目标校验阶段
    就会被拒绝，永远不会读到这些字段。"""
    backup_dir = pathlib.Path(dest) / name
    backup_dir.mkdir(parents=True)
    (backup_dir / "digest.json").write_bytes(
        cass_common.dumps_canonical({"generation": generation})
    )
    if with_complete:
        (backup_dir / "COMPLETE").touch()
    return backup_dir


def _migrate_agents_schema_text(db_path) -> None:
    """模拟一次合法 schema 迁移（如 `ALTER TABLE agents ADD COLUMN x`）对 `agents`
    DDL 文本的影响，直接改写 `sqlite_master.sql`。逐字搬自
    `test_leg34_gate.py::_migrate_agents_schema_text`（brief 明确允许复制这个
    helper）——原因见那边的 docstring：真 `ALTER TABLE` 在这份 synth_dd 文件上
    会触发一个与本模块无关的 SQLite 内部 bug，直接改写 DDL 文本精确达到测试目的
    且不依赖那条不稳定路径。"""
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agents'"
        ).fetchone()
        original_sql = row[0].rstrip()
        assert original_sql.endswith(")"), f"意外的 agents DDL 形态: {original_sql!r}"
        migrated_sql = original_sql[:-1] + ", migrated_x TEXT)"

        con.execute("PRAGMA writable_schema=ON")
        con.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='agents'",
            (migrated_sql,),
        )
        con.execute("PRAGMA writable_schema=RESET")
        con.commit()
    finally:
        con.close()


@pytest.fixture
def gate_baseline(synth_dd, tmp_path):
    """健康 synth_dd 跑一次五腿门作为「上一份已发布备份」（generation=1，链 tip），
    返回 `(db, dest)`。"""
    db = synth_dd / "agent_search.db"
    dest = tmp_path / "dest"
    dest.mkdir()
    census1 = tmp_path / "census1.tsv"
    gate1 = tmp_path / "gate1.json"
    rc, out, err = _run_cli(db, dest, census1, gate1)
    assert rc == 0, f"基线本身不应 FAIL：\nstdout={out}\nstderr={err}"
    _publish_baseline(dest, "cass-baseline", gate1, census1, generation=1)
    return db, dest


# ---------------------------------------------------------------------------
# V5g2b：成对性校验——缺一即 exit 2
# ---------------------------------------------------------------------------


def test_rebaseline_without_reason_exit2(tmp_path):
    db = tmp_path / "nonexistent.db"  # 成对性校验先于开 db，路径不必真实存在
    dest = tmp_path / "dest"
    dest.mkdir()

    rc, out, err = _run_cli(
        db, dest, tmp_path / "census.tsv", tmp_path / "gate.json", rebaseline="cass-x"
    )

    assert rc == 2, f"stdout={out}\nstderr={err}"
    assert "rebaseline" in err.lower() or "rebaseline" in err


def test_rebaseline_reason_without_rebaseline_exit2(tmp_path):
    db = tmp_path / "nonexistent.db"
    dest = tmp_path / "dest"
    dest.mkdir()

    rc, out, err = _run_cli(
        db,
        dest,
        tmp_path / "census.tsv",
        tmp_path / "gate.json",
        rebaseline_reason="给了 reason 但没给 rebaseline",
    )

    assert rc == 2, f"stdout={out}\nstderr={err}"


# ---------------------------------------------------------------------------
# V5g2c：rebaseline 目标三项校验——目录不存在 / 无 COMPLETE / 非 tip
# ---------------------------------------------------------------------------


def test_rebaseline_target_does_not_exist_exit2(tmp_path):
    db = tmp_path / "nonexistent.db"
    dest = tmp_path / "dest"
    dest.mkdir()

    rc, out, err = _run_cli(
        db,
        dest,
        tmp_path / "census.tsv",
        tmp_path / "gate.json",
        rebaseline="cass-does-not-exist",
        rebaseline_reason="x",
    )

    assert rc == 2, f"stdout={out}\nstderr={err}"
    assert "不存在" in err


def test_rebaseline_target_missing_complete_exit2(tmp_path):
    db = tmp_path / "nonexistent.db"
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_bare_published_dir(dest, "cass-nocomplete", generation=1, with_complete=False)

    rc, out, err = _run_cli(
        db,
        dest,
        tmp_path / "census.tsv",
        tmp_path / "gate.json",
        rebaseline="cass-nocomplete",
        rebaseline_reason="x",
    )

    assert rc == 2, f"stdout={out}\nstderr={err}"
    assert "COMPLETE" in err


def test_rebaseline_target_not_tip_even_with_newest_mtime_v15g(tmp_path):
    """V15g：`touch` 一个旧备份目录使其 mtime 最新，指名它做 rebaseline 必须
    FAIL——tip 只看 `generation`，不看 mtime。"""
    db = tmp_path / "nonexistent.db"
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_bare_published_dir(dest, "cass-gen1", generation=1)
    _make_bare_published_dir(dest, "cass-gen2", generation=2)

    # gen2（真正的 tip）先造好之后，把 gen1（非 tip）的 mtime 顶成最新——
    # 用 os.utime 直接设未来时间戳，不依赖系统时钟精度 / sleep。
    gen1_complete = dest / "cass-gen1" / "COMPLETE"
    future = gen1_complete.stat().st_mtime + 10_000
    os.utime(gen1_complete, (future, future))
    assert gen1_complete.stat().st_mtime > (dest / "cass-gen2" / "COMPLETE").stat().st_mtime

    rc, out, err = _run_cli(
        db,
        dest,
        tmp_path / "census.tsv",
        tmp_path / "gate.json",
        rebaseline="cass-gen1",
        rebaseline_reason="x",
    )

    assert rc == 2, f"stdout={out}\nstderr={err}"
    assert "tip" in err
    assert "cass-gen2" in err, "错误信息应指出真正的 tip 是谁"


# ---------------------------------------------------------------------------
# V5g2：rebaseline 不是 bypass-all
# ---------------------------------------------------------------------------


@requires_cass
def test_rebaseline_legit_migration_passes_with_provenance(gate_baseline, tmp_path):
    db, dest = gate_baseline
    _migrate_agents_schema_text(db)

    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"
    reason = "CASS 0.6.18 迁移，schema_version 20→21"

    rc, out, err = _run_cli(
        db,
        dest,
        out_census,
        out_gate,
        rebaseline="cass-baseline",
        rebaseline_reason=reason,
    )

    assert rc == 0, f"stdout={out}\nstderr={err}"
    for i in range(5):
        assert f"[leg {i}] PASS" in out, f"stdout={out}"

    gate = json.loads(out_gate.read_bytes())
    assert gate["rebaselined_from"] == "cass-baseline"
    assert gate["reason"] == reason


@requires_cass
def test_rebaseline_attack1_still_fails_required_objects(gate_baseline, tmp_path):
    """spec §5.7：rebaseline 只关闭「与历史基线的比对」，必需对象清单永不可关。
    用攻击①（删 `meta` 的 schema 条目）跑一次 rebaseline，目标校验本身会通过
    （`cass-baseline` 存在 + 含 `COMPLETE` + 是 tip），但整体门必须仍然 FAIL。"""
    db, dest = gate_baseline
    fixture_factory.attack1(db)

    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"

    rc, out, err = _run_cli(
        db,
        dest,
        out_census,
        out_gate,
        rebaseline="cass-baseline",
        rebaseline_reason="attack1 仍应 FAIL",
    )

    assert rc == 1, f"rebaseline 不是 bypass-all，仍应 FAIL：stdout={out}\nstderr={err}"
    assert "[leg 3] FAIL" in out
    assert "meta" in out
