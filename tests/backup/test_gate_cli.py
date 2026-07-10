"""infra/backup/cass/cass_backup_gate.py 的 CLI 端到端测试（Task 7：五腿门 CLI 组装，
spec §5 全五腿）。

覆盖 Task 7 brief 的 Step 1：

  - 健康合成库 + 无基线 → exit 0，`census.tsv`/`gate.json` 落地且字段齐（首晚登记）。
  - 第二次以第一次的产出为基线 → exit 0（自比对 PASS）。
  - 攻击①–⑦分别跑一次真 CLI 子进程 → exit 1，stdout 用 `[leg N] PASS/FAIL: detail`
    指认正确的腿。

  **V4（攻击①）的重要发现，如实记录（不是本任务范围内可改的 spec 层问题）**：
  spec §9.1 V4 原文断言「整体 FAIL；腿 3 报 meta 缺失；**腿 1/2/4 预期通过**」。
  Task 7 首次把五条腿串成一个真 CLI 进程跑攻击①时，实测证明这半句不成立：
    - 腿 1：攻击①用 `PRAGMA writable_schema` 从 `sqlite_master` 删掉 `meta` 的目录项，
      但**不清理**它的 b-tree 页 ⇒ 遗留孤儿页。`PRAGMA integrity_check` 会吐
      `Page N: never used`（stdout 非空、stderr 为空）——既不匹配签名 A（要求
      `stdout=='ok'`），也不匹配签名 B（要求 stderr 恰好是 malformed 那一行）。
      `classify_integrity` 判定为 FAIL。这是「用 writable_schema 伪造 schema 删除」
      这一构造手法本身的页级副作用，不是 `classify_integrity` 的逻辑错误。
    - 腿 4：`_leg4_watermarks` 的必需水位键检查依赖 `meta` 表本身可读；`meta` 整表
      从 schema 消失后，该表根本查不到 ⇒ 全部必需水位键视为缺失 ⇒ FAIL。这正是
      「必需键存在，rebaseline 也不豁免」这条硬不变式的**正确触发**（Task 7 过程中
      顺带修了一个真 bug：之前 `_leg4_watermarks` 对 `meta` 整体消失会裸抛
      `sqlite3.OperationalError` 崩溃，而不是像腿 3 那样受控 FAIL——已在
      `cass_backup_gate.py` 里补上 `try/except sqlite3.DatabaseError`）。
  结论：V4 的「其余腿预期通过」只对 leg0/leg2 严格成立；leg1/leg4 在这个具体攻击
  构造下也会正确 FAIL——多腿独立命中同一次损坏是纵深防御，不违反设计意图，但与
  spec 表格的字面文本不符，留给人工复核是否需要更新 spec 措辞或攻击①构造。

  V5a（攻击③）不受此影响：`DELETE FROM agents` 是普通 DML，不碰 schema/其它表，
  实测腿 0/1/2/4 确实全部 PASS，与 spec 逐字一致。

  攻击②④⑤⑥⑦只断言命中腿 4 的 FAIL 行（brief 原文口径：「断言 stdout 的
  [leg N] FAIL 行」，不要求断言其余腿状态）。

Step 3：跑一次合成库全门计时，断言 < 30s（合成库远小于生产）。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import time

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
    """跑真 CLI 子进程（不是直接调 Python 函数——e2e 覆盖参数解析 / exit code /
    stdout 指认格式）。"""
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
    """把一次 CLI 跑出的 `gate.json` + `census.tsv` 组装成 `<dest>/<name>/` 下的
    「已发布备份」（含 `COMPLETE` + `digest.json`），供下一次 CLI 调用当基线。这是
    Task 7 brief 建议的最省事造基线手法——第一次跑本身就是权威产出。"""
    backup_dir = pathlib.Path(dest) / name
    backup_dir.mkdir(parents=True)
    shutil.copy(census_path, backup_dir / "census.tsv")
    gate = json.loads(pathlib.Path(gate_json_path).read_bytes())
    gate["generation"] = generation
    (backup_dir / "digest.json").write_bytes(cass_common.dumps_canonical(gate))
    (backup_dir / "COMPLETE").touch()
    return backup_dir


@pytest.fixture
def gate_baseline(synth_dd, tmp_path):
    """健康 synth_dd 跑一次五腿门作为「上一份已发布备份」，返回 `(db, dest)`。"""
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
# 首晚登记：无基线 → exit 0，产物字段齐
# ---------------------------------------------------------------------------


@requires_cass
def test_first_night_no_baseline_passes_and_writes_artifacts(synth_dd, tmp_path):
    db = synth_dd / "agent_search.db"
    dest = tmp_path / "dest"
    dest.mkdir()
    out_census = tmp_path / "census.tsv"
    out_gate = tmp_path / "gate.json"

    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 0, f"stdout={out}\nstderr={err}"
    for i in range(5):
        assert f"[leg {i}] PASS" in out, f"缺 [leg {i}] PASS 行，stdout={out}"

    assert out_census.exists()
    assert out_gate.exists()

    census_text = out_census.read_text(encoding="utf-8")
    assert "agents\t" in census_text
    assert "fts_messages_config\tEXEMPT" in census_text

    gate = json.loads(out_gate.read_bytes())
    assert set(gate) >= {"schema_fingerprint", "tables", "meta_watermarks", "census_sha256"}
    assert "rebaselined_from" not in gate and "reason" not in gate
    for table in ("messages", "conversations"):
        assert table in gate["tables"]
        assert set(gate["tables"][table]) == {"max_id", "count", "prefix_digest"}
    assert gate["census_sha256"] == hashlib.sha256(out_census.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 第二次以第一次为基线：自比对 PASS
# ---------------------------------------------------------------------------


@requires_cass
def test_second_run_against_own_baseline_passes(gate_baseline, tmp_path):
    db, dest = gate_baseline
    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"

    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 0, f"stdout={out}\nstderr={err}"
    for i in range(5):
        assert f"[leg {i}] PASS" in out


# ---------------------------------------------------------------------------
# 攻击①（V4）：整体 FAIL，腿 3 报 meta 缺失/普查消失
# ---------------------------------------------------------------------------


@requires_cass
def test_attack1_meta_missing_v4(gate_baseline, tmp_path):
    db, dest = gate_baseline
    fixture_factory.attack1(db)
    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"

    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "[leg 3] FAIL" in out
    assert "meta" in out
    # 严格成立的「其余腿预期通过」子集（见模块 docstring 的 V4 发现）：
    assert "[leg 0] PASS" in out
    assert "[leg 2] PASS" in out
    # 如实断言实测行为——leg1/leg4 在本攻击构造下也正确 FAIL（发现见 docstring）：
    assert "[leg 1] FAIL" in out
    assert "[leg 4] FAIL" in out
    # FAIL 时产物仍必须落地（SUSPECT 取证需要完整画像，不能因为门 FAIL 就不写）：
    assert out_census.exists()
    assert out_gate.exists()
    assert json.loads(out_gate.read_bytes())["census_sha256"] == hashlib.sha256(
        out_census.read_bytes()
    ).hexdigest()


# ---------------------------------------------------------------------------
# 攻击③（V5a）：agents 清空，腿 3 严格不减 FAIL，其余腿全部 PASS（与 spec 逐字一致）
# ---------------------------------------------------------------------------


@requires_cass
def test_attack3_agents_emptied_v5a(gate_baseline, tmp_path):
    db, dest = gate_baseline
    fixture_factory.attack3(db)
    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"

    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "[leg 3] FAIL" in out
    assert "agents" in out
    assert "[leg 0] PASS" in out
    assert "[leg 1] PASS" in out
    assert "[leg 2] PASS" in out
    assert "[leg 4] PASS" in out


# ---------------------------------------------------------------------------
# 环境错误：--db 路径不存在 → exit 2（回归夹具）
# ---------------------------------------------------------------------------


def test_missing_db_path_exit2(tmp_path):
    """`immutable=1` URI 对不存在的文件不会在 `sqlite3.connect()` 时报错——它会
    静默打开一个空 schema，首条 `SELECT` 才报 `OperationalError: no such table`，
    且发生在 db 连接的 try/except 保护范围之外，会让 CLI 崩成裸 traceback 而不是
    走干净的 exit 2 环境错误路径。这是实现过程中发现的真 bug，已在 `main()` 里
    加一道显式 `db_path.is_file()` 前置检查修掉。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    missing_db = tmp_path / "genuinely-does-not-exist.db"

    rc, out, err = _run_cli(
        missing_db, dest, tmp_path / "census.tsv", tmp_path / "gate.json"
    )

    assert rc == 2, f"stdout={out}\nstderr={err}"
    assert "Traceback" not in err, "应走干净的 exit 2 路径，不应是裸 traceback"
    assert not missing_db.exists(), "存在性检查不应有「顺手创建文件」的副作用"


# ---------------------------------------------------------------------------
# 攻击②④⑤⑥⑦：命中腿 4（brief 口径只断言 [leg 4] FAIL 行）
# ---------------------------------------------------------------------------


@requires_cass
@pytest.mark.parametrize("attack_name", ["attack2", "attack4", "attack5", "attack6", "attack7"])
def test_attacks_targeting_leg4(gate_baseline, tmp_path, attack_name):
    db, dest = gate_baseline
    getattr(fixture_factory, attack_name)(db)
    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"

    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 1, f"{attack_name}: stdout={out}\nstderr={err}"
    assert "[leg 4] FAIL" in out, f"{attack_name}: stdout={out}"
    assert out_census.exists(), f"{attack_name}: FAIL 时产物仍必须落地（SUSPECT 取证）"
    assert out_gate.exists(), f"{attack_name}: FAIL 时产物仍必须落地（SUSPECT 取证）"


# ---------------------------------------------------------------------------
# Step 3：合成库全门计时 < 30s（合成库远小于生产；生产 <6s 验收在 Tier B）
# ---------------------------------------------------------------------------


@requires_cass
def test_full_gate_synthetic_db_runs_under_30s(synth_dd, tmp_path):
    db = synth_dd / "agent_search.db"
    dest = tmp_path / "dest"
    dest.mkdir()
    out_census = tmp_path / "census.tsv"
    out_gate = tmp_path / "gate.json"

    start = time.monotonic()
    rc, out, err = _run_cli(db, dest, out_census, out_gate)
    elapsed = time.monotonic() - start

    assert rc == 0, f"stdout={out}\nstderr={err}"
    assert elapsed < 30, f"合成库全门耗时 {elapsed:.2f}s 超过 30s 预算"
