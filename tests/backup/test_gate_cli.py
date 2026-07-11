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
import sqlite3
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


@requires_cass
def test_first_night_bad_watermark_format_fails_gate_r9(synth_dd, tmp_path):
    """codex R9-P0（首晚复现）：全新 DEST（无基线，首晚登记模式）+ `last_scan_ts`
    改「同数字 + 尾随 \\n」→ gate CLI 必须 exit 1、`[leg 4] FAIL`。修复前水位格式
    校验只在「与 prev 比较」分支跑，首晚走「[leg 4] PASS 首晚登记」、发布含 COMPLETE
    的 cass-*（digest 里保留尾随换行）。水位格式合法性是无条件不变式（spec §5.5(b)）。"""
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        cur_val = con.execute("SELECT value FROM meta WHERE key='last_scan_ts'").fetchone()[0]
        con.execute("UPDATE meta SET value=? WHERE key='last_scan_ts'", (f"{cur_val}\n",))
        con.commit()
    finally:
        con.close()

    dest = tmp_path / "dest"
    dest.mkdir()  # 空 dest = 首晚，无基线
    out_census = tmp_path / "census.tsv"
    out_gate = tmp_path / "gate.json"

    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 1, f"首晚坏水位必须 FAIL（格式校验无条件）：stdout={out}\nstderr={err}"
    assert "[leg 4] FAIL" in out, out
    assert "last_scan_ts" in out and "解析失败" in out, out
    # 首晚登记的 PASS 措辞不该出现在腿4（证明没走「登记放行」老路）：
    assert "[leg 4] PASS" not in out, out
    # 产物仍落地（SUSPECT 取证契约）：
    assert out_census.exists() and out_gate.exists()


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
# 单腿崩溃防御（review 修复）：缺 messages 表 / 任意腿抛异常都不得击穿产物落盘
# ---------------------------------------------------------------------------


def test_db_without_messages_table_fails_controlled_with_artifacts(tmp_path):
    """review 实测复现的修复回归：缺 `messages` 表的库曾让 CLI 在 leg0 处裸
    traceback 崩溃——零产物、无 [leg N] 行，违反「不短路 + 产物无论 PASS/FAIL
    都写」契约。修后：leg0 受控 FAIL（sqlite3.DatabaseError 捕获），五条腿全部
    打印，census/gate.json 照落盘，exit 1。"""
    db = tmp_path / "no-messages.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT)")
    con.execute("INSERT INTO conversations (title) VALUES ('x')")
    con.commit()
    con.close()
    dest = tmp_path / "dest"
    dest.mkdir()
    out_census = tmp_path / "census.tsv"
    out_gate = tmp_path / "gate.json"

    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "Traceback" not in err, "单腿失败必须受控，不允许裸 traceback"
    for i in range(5):
        assert f"[leg {i}] " in out, f"缺 [leg {i}] 行（不短路契约），stdout={out}"
    assert "[leg 0] FAIL" in out
    assert "messages" in out
    assert out_census.exists(), "FAIL 时 census.tsv 仍必须落地"
    assert out_gate.exists(), "FAIL 时 gate.json 仍必须落地"
    gate = json.loads(out_gate.read_bytes())
    assert set(gate) >= {"schema_fingerprint", "tables", "meta_watermarks", "census_sha256"}


@requires_cass
def test_top_level_safety_net_degrades_crashing_leg_to_fail(synth_dd, tmp_path, monkeypatch, capsys):
    """顶层安全网：任何单腿抛出任何异常（不只 sqlite3 系）→ 该腿降级 FAIL
    （detail 含异常类型+文本），其余腿照跑、产物照写、exit 1。

    monkeypatch 无法穿透 subprocess，本测试按 review 裁决直接调 `main()` 函数级
    验证（模块内 `main` 按全局名查找 `leg2`，monkeypatch 生效）。"""
    import cass_backup_gate

    def _boom(con):
        raise RuntimeError("synthetic leg2 crash for safety-net test")

    monkeypatch.setattr(cass_backup_gate, "leg2", _boom)

    db = synth_dd / "agent_search.db"
    dest = tmp_path / "dest"
    dest.mkdir()
    out_census = tmp_path / "census.tsv"
    out_gate = tmp_path / "gate.json"

    rc = cass_backup_gate.main(
        [
            "--db",
            str(db),
            "--dest",
            str(dest),
            "--out-census",
            str(out_census),
            "--out-gate-json",
            str(out_gate),
        ]
    )
    out = capsys.readouterr().out

    assert rc == 1
    assert "[leg 2] FAIL" in out
    assert "RuntimeError" in out, "detail 必须含异常类型"
    assert "synthetic leg2 crash" in out, "detail 必须含异常文本"
    # 其余腿照跑（健康合成库上应 PASS）：
    for i in (0, 1, 3, 4):
        assert f"[leg {i}] PASS" in out, f"stdout={out}"
    assert out_census.exists(), "单腿崩溃不得击穿产物落盘承诺"
    assert out_gate.exists(), "单腿崩溃不得击穿产物落盘承诺"


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
# codex R2-P0：基线「全有或全无」校验——三种复现（不需要额外攻击当前 db，毒
# 基线本身就该被 `_validate_baseline` 拦下）+ rebaseline 逃生门回归。
# ---------------------------------------------------------------------------


@requires_cass
def test_baseline_census_line_removed_fails_census_binding(gate_baseline, tmp_path):
    """复现①：直接删掉基线 `census.tsv` 里的一行（`agents`）——即使当前 db
    的 `agents` 完全没变，census.tsv 与 digest.json 记录的 `census_sha256` 已经
    对不上，必须被 `_validate_baseline` 拦下（不能像修复前那样：因为
    `_leg3_compare_census` 只遍历 `prev_census.items()`，缺的键根本不会被拿来
    比对，静默放行）。"""
    db, dest = gate_baseline
    census_path = dest / "cass-baseline" / "census.tsv"
    lines = census_path.read_text(encoding="utf-8").splitlines(keepends=True)
    tampered = [line for line in lines if not line.startswith("agents\t")]
    assert len(tampered) < len(lines), "夹具自检：census.tsv 应含 agents 行"
    census_path.write_text("".join(tampered), encoding="utf-8")

    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"
    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "[baseline] FAIL" in out, out
    assert "census" in out, out
    assert "需人工 rebaseline" in out, out
    # 五腿仍必须跑完、产物仍必须落地（不因基线校验失败就短路五腿门本身）：
    for i in range(5):
        assert f"[leg {i}] " in out, out
    assert out_census.exists()
    assert out_gate.exists()


@requires_cass
def test_baseline_digest_missing_tables_messages_fails(gate_baseline, tmp_path):
    """复现②：基线 `digest.json` 挖掉 `tables.messages`（不需要改写当前 db 的
    消息内容来演示——结构缺失本身就该被拦下：`leg4` 对 `prev_tables.get(table)`
    取到 `None` 时会当成「该表首晚登记」，跳过前缀摘要/单调性比对，静默放行
    行内容篡改）。"""
    db, dest = gate_baseline
    digest_path = dest / "cass-baseline" / "digest.json"
    digest = json.loads(digest_path.read_bytes())
    del digest["tables"]["messages"]
    digest_path.write_bytes(cass_common.dumps_canonical(digest))

    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"
    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "[baseline] FAIL" in out, out
    assert "tables" in out and "messages" in out, out
    assert "需人工 rebaseline" in out, out


@requires_cass
def test_baseline_meta_watermarks_missing_last_scan_ts_fails(gate_baseline, tmp_path):
    """复现③：基线 `digest.json` 的 `meta_watermarks` 挖掉 `last_scan_ts`——
    `_leg4_watermarks` 的单调性比对逐键跳过「prev 里没有」的键，缺键会让水位
    回退检查对那一个键彻底失效（当前必需键存在性检查只看当前 db，不看基线）。"""
    db, dest = gate_baseline
    digest_path = dest / "cass-baseline" / "digest.json"
    digest = json.loads(digest_path.read_bytes())
    del digest["meta_watermarks"]["last_scan_ts"]
    digest_path.write_bytes(cass_common.dumps_canonical(digest))

    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"
    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "[baseline] FAIL" in out, out
    assert "meta_watermarks" in out and "last_scan_ts" in out, out
    assert "需人工 rebaseline" in out, out


@requires_cass
def test_baseline_prefix_digest_trailing_newline_fails_r7(gate_baseline, tmp_path):
    """codex R7 同族硬化（`_HEX64_RE.match()`→`.fullmatch()`）：基线 digest.json 的
    `tables.messages.prefix_digest` 尾随一个 \\n（65 字节）。旧 `^[0-9a-f]{64}$` +
    `.match()` 会放它过结构门（`$` 匹配到 \\n 之前）→ 结构校验假通过，只在腿 4 才
    以「前缀摘要不符」间接暴露；fullmatch 后 `_validate_baseline` 直接在结构门指认
    `prefix_digest 非法`，响亮报「需人工 rebaseline」。"""
    db, dest = gate_baseline
    digest_path = dest / "cass-baseline" / "digest.json"
    digest = json.loads(digest_path.read_bytes())
    digest["tables"]["messages"]["prefix_digest"] = (
        digest["tables"]["messages"]["prefix_digest"] + "\n"
    )
    digest_path.write_bytes(cass_common.dumps_canonical(digest))

    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"
    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "[baseline] FAIL" in out, out
    assert "prefix_digest" in out and "非法" in out, out
    assert "需人工 rebaseline" in out, out


@requires_cass
def test_baseline_rebaseline_escape_hatch_bypasses_validation(gate_baseline, tmp_path):
    """逃生门回归：同一份毒基线（挖掉 `meta_watermarks.last_scan_ts`），用
    `--rebaseline` 指名它（它仍是链 tip，`_validate_rebaseline_target` 三项校验
    只看目录存在 / COMPLETE / tip，不深入 digest 内容）+ reason，必须照常跑通
    ——rebaseline 模式跳过 `_validate_baseline`（spec §5.7：与硬编码不变式的比对
    永不可关，但与历史基线的比对本来就该被 rebaseline 关掉，这正是它存在的
    理由）。"""
    db, dest = gate_baseline
    digest_path = dest / "cass-baseline" / "digest.json"
    digest = json.loads(digest_path.read_bytes())
    del digest["meta_watermarks"]["last_scan_ts"]
    digest_path.write_bytes(cass_common.dumps_canonical(digest))

    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"
    rc, out, err = _run_cli(
        db, dest, out_census, out_gate,
        rebaseline="cass-baseline", rebaseline_reason="毒基线逃生门回归测试",
    )

    assert rc == 0, f"stdout={out}\nstderr={err}"
    assert "[baseline] FAIL" not in out, out
    for i in range(5):
        assert f"[leg {i}] PASS" in out, out
    gate = json.loads(out_gate.read_bytes())
    assert gate["rebaselined_from"] == "cass-baseline"


# ---------------------------------------------------------------------------
# codex R4-P0：基线选择 strict——真实 tip 不可读时绝不静默退回更老基线放行。
# ---------------------------------------------------------------------------


@requires_cass
def test_gate_unreadable_newer_tip_fails_not_silently_uses_older(gate_baseline, tmp_path):
    """codex 复现：gen1 有效基线（cass-baseline）+ 一个更新的 cass-*（含 COMPLETE
    但 generation 坏）。修复前 latest_published 宽容跳过坏 tip、退回 gen1 比对，
    当前 db（未变）→ 五腿全 PASS、rc=0（把「真实上一份不可读」当没发生）。修复后
    strict 选择 raise → gate 打 [baseline] FAIL、强制 rc=1，五腿仍全部打印、产物
    仍落地（SUSPECT 取证）。"""
    db, dest = gate_baseline  # cass-baseline generation=1，结构完整

    newer = dest / "cass-newer-brokengen"
    newer.mkdir()
    (newer / "digest.json").write_bytes(
        cass_common.dumps_canonical({"generation": "2"})  # 字符串，非法
    )
    (newer / "COMPLETE").touch()

    out_census = tmp_path / "census2.tsv"
    out_gate = tmp_path / "gate2.json"
    rc, out, err = _run_cli(db, dest, out_census, out_gate)

    assert rc == 1, f"真实 tip 不可读必须 FAIL，不得静默退回 gen1：stdout={out}\nstderr={err}"
    assert "[baseline] FAIL" in out, out
    assert "cass-newer-brokengen" in out, out
    assert "基线集不可信" in out or "需人工" in out, out
    # 五腿仍跑完、产物仍落地（不因基线不可信就短路五腿门本身）：
    for i in range(5):
        assert f"[leg {i}] " in out, out
    assert out_census.exists()
    assert out_gate.exists()


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
