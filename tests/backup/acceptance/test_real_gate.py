"""Tier B acceptance — DB 五腿门在真实生产对照物上的验收（spec §9.1 V1–V5d3③）+
生成器对拍（task-18 brief Step 2）。

依赖 `tests/backup/acceptance/conftest.py` 的 `fixtures_dir`/`corrupt_bak_path`
两个 session fixture（`CASS_BACKUP_FIXTURES` 缺失时整组 skip，见该文件）。

**跑法**：真 subprocess 跑 `infra/backup/cass/cass_backup_gate.py`（而非直接调用
python 内部函数）——这是「门」本身，端到端覆盖 argparse / rebaseline 校验 / 产物
落盘 / exit code，与 Tier A 的 `test_gate_cli.py` 同一约定。全部 2.3G 级生产库以
`--db` 直接指向 `$CASS_BACKUP_FIXTURES` 下的原始文件（gate 内部用
`file:...?immutable=1` URI 只读打开，不需要 cp 副本——brief 明确要求「gate 跑只读
的直接用原文件」，只有 Step 2 的生成器对拍需要可写副本，且用完立即删）。

**关于 V4 的经验性发现（task-18 brief「OPEN-DECISION #1」）**：spec §9.1 V4 字面
写「腿1/2/4 预期通过」，但 Task 7（`tests/backup/test_gate_cli.py` 的
`test_attack1_meta_missing_v4`）在**合成库**上经验证实：腿1 也 FAIL（`writable_schema`
删表法留下的孤立页让 `integrity_check` 输出「Page N: never used」，不匹配签名A/B
任一），腿4 同样 FAIL（`meta` 整表不可读，必需水位键判定为全部缺失）——只有腿0/2
PASS。本文件在**真实生产库**（`probe-snapshot.db` 已知天生签名B）上重跑同一攻击，
经验证实腿1 的行为与合成库不同：`probe-snapshot.db` 本身已经因既有的
`fts_messages_config` 缺陷天生输出签名B（孤立页 + 那一行 malformed stderr），
删 `meta` 不额外引入新的孤立页模式，`integrity_check` 输出形态不变，仍精确匹配
签名B ⇒ 腿1 PASS。腿4 则与合成库结论一致（`meta` 消失⇒必需水位键全缺⇒FAIL）。
两次经验证实合起来的结论：**spec V4 字面「腿1/2/4 预期通过」在两种库上都不成立**
（腿4 从未 PASS 过），「腿1 是否 PASS」取决于攻击构造方式与目标库的既有 b-tree
状态，不是这条攻击的固有性质。本文件如实断言真实观测（不强造字面 spec），并在此
记录供人工 / codex 复核是否需要更新 spec 原文或 V4 攻击构造本身。
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import sqlite3
import statistics
import subprocess
import time

import pytest

import fixture_factory
from cass_backup_gate import classify_integrity, leg0, run_integrity_check

REPO = pathlib.Path(__file__).resolve().parent.parent.parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
GATE_SCRIPT = REPO / "infra" / "backup" / "cass" / "cass_backup_gate.py"

_LEG_LINE_RE = re.compile(r"^\[leg (\d)\] (PASS|FAIL)", re.MULTILINE)


def _run_gate_cli(db_path, dest, out_census, out_gate_json, timeout=60) -> tuple[int, str, str]:
    cmd = [
        str(VENV_PY), str(GATE_SCRIPT),
        "--db", str(db_path),
        "--dest", str(dest),
        "--out-census", str(out_census),
        "--out-gate-json", str(out_gate_json),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def _leg_verdicts(stdout: str) -> dict[int, str]:
    """解析 CLI stdout 的 `[leg N] PASS/FAIL: ...` 行，返回 `{腿号: "PASS"|"FAIL"}`。"""
    return {int(n): verdict for n, verdict in _LEG_LINE_RE.findall(stdout)}


def _seed_baseline(db_path: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    """在 `dest` 下用 `db_path`（首晚登记模式）建一个满足
    `cass_common.latest_published` 契约的最小「已发布」目录
    （`cass-baseline/{COMPLETE,census.tsv,digest.json}`），供「第二晚比对」类
    攻击测试（V5/V5a/V5b/V5c/V5d/V5d4）复用。只含 `main()` 读取所需的四个键
    （`generation`/`schema_fingerprint`/`tables`/`meta_watermarks`）——本文件只测
    五腿门本体，不是 `backup-cass.sh` 全脚本 e2e（那是 Tier A 的 `test_first_night.py`
    覆盖的范围）。
    """
    dest.mkdir(parents=True, exist_ok=True)
    census = dest / "_seed_census.tsv"
    gate_json = dest / "_seed_gate.json"
    rc, out, err = _run_gate_cli(db_path, dest, census, gate_json, timeout=60)
    assert rc == 0, f"baseline seed 失败（应为首晚登记 PASS）：\nSTDOUT={out}\nSTDERR={err}"

    gate = json.loads(gate_json.read_bytes())
    baseline_dir = dest / "cass-baseline"
    baseline_dir.mkdir()
    shutil.copy(census, baseline_dir / "census.tsv")
    (baseline_dir / "COMPLETE").touch()
    digest = {
        "generation": 1,
        "schema_fingerprint": gate["schema_fingerprint"],
        "tables": gate["tables"],
        "meta_watermarks": gate["meta_watermarks"],
    }
    (baseline_dir / "digest.json").write_bytes(json.dumps(digest).encode())
    return baseline_dir


# ---------------------------------------------------------------------------
# V1 — 干净新建库（含 FTS5 vtab）：腿1 命中签名A；腿0 因空表 FAIL（防呆机制的
# 正确行为，不强造整门 PASS——task-18 brief 明确授权拆成两半断言）。
# ---------------------------------------------------------------------------


def test_v1_fresh_fts_signature_a_and_leg0_correctly_rejects_empty_db(fixtures_dir):
    db_path = fixtures_dir / "fresh-fts.db"
    assert db_path.is_file(), f"缺 fresh-fts.db：{db_path}"

    stdout, stderr, exit_code = run_integrity_check(db_path)
    assert classify_integrity(stdout, stderr, exit_code) == "A"

    con = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
    try:
        result = leg0(con)
    finally:
        con.close()
    # fresh-fts.db 是真正的空库（连 messages/conversations 表本身都不存在，不只是
    # 空表）——腿0 的防呆 COUNT 受控 FAIL（不是裸 crash），这正是「count==0 即通过
    # 这一整类假绿」的反面：一个全新初始化但从未摄入过内容的库不该被当成健康备份
    # 对照物看待。
    assert result.ok is False
    assert "no such table" in result.detail


# ---------------------------------------------------------------------------
# V2 — 当晚的健康快照：腿1 命中签名B；整体（首晚登记模式）PASS；全门计时 < 6s
# （spec §5 口径，跑 3 次取中位）。
# ---------------------------------------------------------------------------


def test_v2_probe_snapshot_signature_b_full_gate_pass_and_under_6s_median(fixtures_dir, tmp_path):
    db_path = fixtures_dir / "probe-snapshot.db"
    assert db_path.is_file(), f"缺 probe-snapshot.db：{db_path}"

    stdout, stderr, exit_code = run_integrity_check(db_path)
    assert classify_integrity(stdout, stderr, exit_code) == "B"

    elapsed_runs = []
    for i in range(3):
        run_dest = tmp_path / f"dest-{i}"
        run_dest.mkdir()
        census = tmp_path / f"census-{i}.tsv"
        gate_json = tmp_path / f"gate-{i}.json"
        t0 = time.monotonic()
        rc, out, err = _run_gate_cli(db_path, run_dest, census, gate_json, timeout=30)
        elapsed_runs.append(time.monotonic() - t0)
        assert rc == 0, f"run {i} 应为首晚登记 PASS：\nSTDOUT={out}\nSTDERR={err}"
        assert _leg_verdicts(out) == {0: "PASS", 1: "PASS", 2: "PASS", 3: "PASS", 4: "PASS"}

    median_elapsed = statistics.median(elapsed_runs)
    assert median_elapsed < 6.0, (
        f"全门计时中位数 {median_elapsed:.2f}s 超出 spec §5 的 6s 预算"
        f"（3 次实测：{[f'{e:.2f}s' for e in elapsed_runs]}）"
    )


# ---------------------------------------------------------------------------
# V3 — 生产 corrupt-bak（Rowid 905 out of order）：整体 FAIL；腿2 的
# conversations 子腿 = 1。
# ---------------------------------------------------------------------------


def test_v3_corrupt_bak_fails_with_leg2_conversations_divergence_of_1(corrupt_bak_path, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    census = tmp_path / "census.tsv"
    gate_json = tmp_path / "gate.json"
    rc, out, err = _run_gate_cli(corrupt_bak_path, dest, census, gate_json, timeout=60)

    assert rc == 1, f"corrupt-bak 应整体 FAIL：\nSTDOUT={out}\nSTDERR={err}"
    verdicts = _leg_verdicts(out)
    assert verdicts[2] == "FAIL"
    leg2_line = next(line for line in out.splitlines() if line.startswith("[leg 2]"))
    assert '"conversations"' in leg2_line
    assert "分歧=1" in leg2_line


# ---------------------------------------------------------------------------
# V5d3③ — 真库半句：probe-snapshot.db 的 extra_bin 含 0x1F 与 0x1E 的行数均 > 0
# （证明分隔符字节冲突不是理论问题）。
# ---------------------------------------------------------------------------


def test_v5d3_3_real_extra_bin_contains_separator_bytes(fixtures_dir):
    db_path = fixtures_dir / "probe-snapshot.db"
    con = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
    try:
        count_1f = con.execute(
            "SELECT COUNT(*) FROM messages WHERE extra_bin IS NOT NULL AND instr(extra_bin, X'1F') > 0"
        ).fetchone()[0]
        count_1e = con.execute(
            "SELECT COUNT(*) FROM messages WHERE extra_bin IS NOT NULL AND instr(extra_bin, X'1E') > 0"
        ).fetchone()[0]
    finally:
        con.close()
    assert count_1f > 0, "0x1F 分隔符字节应在真实生产 extra_bin 里天然出现"
    assert count_1e > 0, "0x1E 分隔符字节应在真实生产 extra_bin 里天然出现"


# ---------------------------------------------------------------------------
# 攻击库①–⑦（保险副本）—— 以 probe-snapshot.db 的首晚登记为基线，跑「第二晚」。
# 共享同一份 session 级基线（省 6 次重复的 ~4s 首晚登记开销）。
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def probe_baseline_dest(fixtures_dir, tmp_path_factory):
    dest = tmp_path_factory.mktemp("v5-baseline-dest")
    _seed_baseline(fixtures_dir / "probe-snapshot.db", dest)
    return dest


def test_v4_attack1_meta_deleted_from_schema(fixtures_dir, probe_baseline_dest, tmp_path):
    """攻击库①（`attack4.db`）：`meta` 从 schema 删除。整体 FAIL；腿3 报「meta
    缺失/读不动」；腿4 同样 FAIL（meta 不可读）。腿1 在真库上如实观测为 PASS
    （签名B——见模块 docstring「关于 V4 的经验性发现」）。"""
    db_path = fixtures_dir / "attack4.db"
    census = tmp_path / "census.tsv"
    gate_json = tmp_path / "gate.json"
    rc, out, err = _run_gate_cli(db_path, probe_baseline_dest, census, gate_json, timeout=60)

    assert rc == 1, f"攻击①应整体 FAIL：\nSTDOUT={out}\nSTDERR={err}"
    verdicts = _leg_verdicts(out)
    assert verdicts[0] == "PASS"
    assert verdicts[1] == "PASS"  # 如实断言：真库上仍命中签名B，见模块 docstring
    assert verdicts[2] == "PASS"
    assert verdicts[3] == "FAIL"
    assert verdicts[4] == "FAIL"
    assert '"meta"' in out
    assert "integrity_check signature=B" in out


def test_v5a_attack3_agents_table_emptied(fixtures_dir, probe_baseline_dest, tmp_path):
    """攻击库③（`attack5-empty-agents.db`）：`agents` 表清空，不动 schema。
    整体 FAIL；腿3 的全表普查报行数减少（严格不减）。腿0/1/2/4 PASS——与 spec
    §9.1 V5a 字面一致（经验证实）。"""
    db_path = fixtures_dir / "attack5-empty-agents.db"
    census = tmp_path / "census.tsv"
    gate_json = tmp_path / "gate.json"
    rc, out, err = _run_gate_cli(db_path, probe_baseline_dest, census, gate_json, timeout=60)

    assert rc == 1, f"攻击③应整体 FAIL：\nSTDOUT={out}\nSTDERR={err}"
    verdicts = _leg_verdicts(out)
    assert verdicts == {0: "PASS", 1: "PASS", 2: "PASS", 3: "FAIL", 4: "PASS"}
    leg3_line = next(line for line in out.splitlines() if line.startswith("[leg 3]"))
    assert '"agents"' in leg3_line
    assert "行数减少" in leg3_line


def test_v5_attack2_message_content_zeroed(fixtures_dir, probe_baseline_dest, tmp_path):
    """攻击库②（`attack6-zeroed-content.db`）：清空最多 1000 条
    `messages.content`。整体 FAIL；腿4 全列前缀摘要不符。腿0/1/2/3 PASS——与 spec
    §9.1 V5 字面一致（经验证实）。"""
    db_path = fixtures_dir / "attack6-zeroed-content.db"
    census = tmp_path / "census.tsv"
    gate_json = tmp_path / "gate.json"
    rc, out, err = _run_gate_cli(db_path, probe_baseline_dest, census, gate_json, timeout=60)

    assert rc == 1, f"攻击②应整体 FAIL：\nSTDOUT={out}\nSTDERR={err}"
    verdicts = _leg_verdicts(out)
    assert verdicts == {0: "PASS", 1: "PASS", 2: "PASS", 3: "PASS", 4: "FAIL"}
    leg4_line = next(line for line in out.splitlines() if line.startswith("[leg 4]"))
    assert "前缀摘要不符" in leg4_line


def test_v5b_attack4_author_column_only(fixtures_dir, probe_baseline_dest, tmp_path):
    """攻击库④（`attack7-author.db`）：只改 `messages.author` 一列。整体 FAIL；
    腿4 抓到（全列摘要含 author 列）。腿0/1/2/3 PASS——与 spec §9.1 V5b 字面一致
    （经验证实）。"""
    db_path = fixtures_dir / "attack7-author.db"
    census = tmp_path / "census.tsv"
    gate_json = tmp_path / "gate.json"
    rc, out, err = _run_gate_cli(db_path, probe_baseline_dest, census, gate_json, timeout=60)

    assert rc == 1, f"攻击④应整体 FAIL：\nSTDOUT={out}\nSTDERR={err}"
    verdicts = _leg_verdicts(out)
    assert verdicts == {0: "PASS", 1: "PASS", 2: "PASS", 3: "PASS", 4: "FAIL"}
    leg4_line = next(line for line in out.splitlines() if line.startswith("[leg 4]"))
    assert "前缀摘要不符" in leg4_line


def test_v5c_attack5_tail_shrink_1000_rows(fixtures_dir, probe_baseline_dest, tmp_path):
    """攻击库⑤（`attack8-suffix.db`）：净缩尾——删掉尾部 1000 行（保持 gap=0）。
    整体 FAIL；腿4 的 `MAX(id)>=prev` 与 `COUNT>=prev` 单调性判据抓到（spec 原文
    call 出的判据）。**经验补充**：腿3 的全表严格不减普查对 `messages` 表同样
    触发（1000/213195≈0.47% 的净减在「严格不减」——而非百分比阈值——判据下同样
    是 FAIL），与 spec 关于「单调性判据是唯一防线」的设计意图并不矛盾（spec 未对
    V5c 明确宣称「腿3预期通过」，这条只是本次真实数据规模下的额外真实观测）。"""
    db_path = fixtures_dir / "attack8-suffix.db"
    census = tmp_path / "census.tsv"
    gate_json = tmp_path / "gate.json"
    rc, out, err = _run_gate_cli(db_path, probe_baseline_dest, census, gate_json, timeout=60)

    assert rc == 1, f"攻击⑤应整体 FAIL：\nSTDOUT={out}\nSTDERR={err}"
    verdicts = _leg_verdicts(out)
    assert verdicts[0] == "PASS"
    assert verdicts[1] == "PASS"
    assert verdicts[2] == "PASS"
    assert verdicts[4] == "FAIL"
    leg4_line = next(line for line in out.splitlines() if line.startswith("[leg 4]"))
    assert "max_id 回退" in leg4_line
    assert "count 回退" in leg4_line


def test_v5d_attack6_watermark_regressed(fixtures_dir, probe_baseline_dest, tmp_path):
    """攻击库⑥（`attack9-watermark.db`）：`meta.last_scan_ts` 改小。整体 FAIL；
    水位单调性抓到（腿4）。断言 schema 指纹/行数普查/前缀摘要全部不受影响
    （腿3 PASS，且腿4 的问题只出现在水位键，不涉及 messages/conversations 前缀）
    ——与 spec §9.1 V5d 字面一致（经验证实）。"""
    db_path = fixtures_dir / "attack9-watermark.db"
    census = tmp_path / "census.tsv"
    gate_json = tmp_path / "gate.json"
    rc, out, err = _run_gate_cli(db_path, probe_baseline_dest, census, gate_json, timeout=60)

    assert rc == 1, f"攻击⑥应整体 FAIL：\nSTDOUT={out}\nSTDERR={err}"
    verdicts = _leg_verdicts(out)
    assert verdicts == {0: "PASS", 1: "PASS", 2: "PASS", 3: "PASS", 4: "FAIL"}
    leg3_line = next(line for line in out.splitlines() if line.startswith("[leg 3]"))
    assert "全表普查 PASS" in leg3_line
    assert "schema 指纹一致" in leg3_line
    leg4_line = next(line for line in out.splitlines() if line.startswith("[leg 4]"))
    assert "last_scan_ts" in leg4_line
    assert "回退" in leg4_line
    assert "前缀摘要不符" not in leg4_line  # 只是水位回退，不是内容改写


def test_v5d4_attack7_last_scan_ts_row_deleted(fixtures_dir, probe_baseline_dest, tmp_path):
    """攻击库⑦（`attack10-meta-row-deleted.db`）：删掉 `meta` 里 `last_scan_ts`
    整行。整体 FAIL；腿4 的「必需水位键存在」判据抓到（spec 原文 call 出的判据，
    对 spec 的「这是唯一能拦住它的判据」论证——该论证针对的是一个**假设的**
    百分比阈值实现，不是本仓当前「严格不减」实现的行为断言）。**经验补充**：
    `meta` 表本身在真库上只有 9 行，删掉 1 行是 9→8 的行数变化，在本仓「严格不减
	（非百分比阈值）」的腿3普查下同样会被判 FAIL——如实记录，不强造腿3 PASS。"""
    db_path = fixtures_dir / "attack10-meta-row-deleted.db"
    census = tmp_path / "census.tsv"
    gate_json = tmp_path / "gate.json"
    rc, out, err = _run_gate_cli(db_path, probe_baseline_dest, census, gate_json, timeout=60)

    assert rc == 1, f"攻击⑦应整体 FAIL：\nSTDOUT={out}\nSTDERR={err}"
    verdicts = _leg_verdicts(out)
    assert verdicts[0] == "PASS"
    assert verdicts[1] == "PASS"
    assert verdicts[2] == "PASS"
    assert verdicts[4] == "FAIL"
    leg4_line = next(line for line in out.splitlines() if line.startswith("[leg 4]"))
    assert "必需水位键缺失" in leg4_line
    assert "last_scan_ts" in leg4_line


# ---------------------------------------------------------------------------
# Step 2 —— 生成器对拍：fixture_factory.attack1..7 应用到 probe-snapshot.db 的
# 可写副本，与保险副本 attack*.db 跑同一个五腿门，比较逐腿 verdict（不比字节）。
# 通过即达成「保险副本可删」的条件（删不删由人工决定，本测试只提供证据）。
# 串行执行、跑完一个攻击立刻删可写副本（2.3G 级文件，磁盘保护）。
# ---------------------------------------------------------------------------

_ATTACK_CASES = [
    ("attack1", "attack4.db", "V4/攻击①(meta删除)"),
    ("attack2", "attack6-zeroed-content.db", "V5/攻击②(content清空)"),
    ("attack3", "attack5-empty-agents.db", "V5a/攻击③(agents清空)"),
    ("attack4", "attack7-author.db", "V5b/攻击④(author改写)"),
    ("attack5", "attack8-suffix.db", "V5c/攻击⑤(净缩尾)"),
    ("attack6", "attack9-watermark.db", "V5d/攻击⑥(水位改小)"),
    ("attack7", "attack10-meta-row-deleted.db", "V5d4/攻击⑦(水位行删除)"),
]


@pytest.mark.slow
def test_step2_generator_matches_insurance_copies_per_leg_verdict(fixtures_dir, probe_baseline_dest, tmp_path):
    probe_db = fixtures_dir / "probe-snapshot.db"
    mismatches: list[tuple[str, dict, dict]] = []

    for attack_fn_name, insurance_name, label in _ATTACK_CASES:
        attack_fn = getattr(fixture_factory, attack_fn_name)
        insurance_path = fixtures_dir / insurance_name
        assert insurance_path.is_file(), f"{label}: 保险副本缺失 {insurance_path}"

        # 1) 保险副本跑门（复用共享 session 基线，独立产物路径避免与其它测试撞名）。
        ins_census = tmp_path / f"ins-census-{attack_fn_name}.tsv"
        ins_gate_json = tmp_path / f"ins-gate-{attack_fn_name}.json"
        ins_rc, ins_out, ins_err = _run_gate_cli(
            insurance_path, probe_baseline_dest, ins_census, ins_gate_json, timeout=60
        )
        insurance_verdicts = _leg_verdicts(ins_out)
        assert insurance_verdicts, f"{label}: 保险副本跑门未产出 [leg N] 行，rc={ins_rc}\n{ins_out}\n{ins_err}"

        # 2) 生成器产出的副本：cp probe-snapshot → 应用 attack_fn → 跑门 → 立刻删。
        generated_copy = tmp_path / "generated.db"
        shutil.copyfile(probe_db, generated_copy)
        try:
            if attack_fn_name == "attack5":
                # 对齐保险副本 attack8-suffix.db 的实际构造幅度（净删尾部 1000 行），
                # 而不是 attack5() 的默认 N/3（那是给合成小库设计的比例缩放）。
                attack_fn(generated_copy, n_rows=1000)
            else:
                attack_fn(generated_copy)

            gen_census = tmp_path / f"gen-census-{attack_fn_name}.tsv"
            gen_gate_json = tmp_path / f"gen-gate-{attack_fn_name}.json"
            gen_rc, gen_out, gen_err = _run_gate_cli(
                generated_copy, probe_baseline_dest, gen_census, gen_gate_json, timeout=60
            )
            generated_verdicts = _leg_verdicts(gen_out)
            assert generated_verdicts, f"{label}: 生成副本跑门未产出 [leg N] 行，rc={gen_rc}\n{gen_out}\n{gen_err}"
        finally:
            generated_copy.unlink(missing_ok=True)

        if insurance_verdicts != generated_verdicts:
            mismatches.append((label, insurance_verdicts, generated_verdicts))

    assert not mismatches, (
        "生成器（fixture_factory）与保险副本（attack*.db）逐腿 verdict 不一致，"
        f"「保险副本可删」条件未达成：{mismatches}"
    )
