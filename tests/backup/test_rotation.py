"""`infra/backup/backup-cass.sh` step 16-17 的测试（Task 14：keep-N 轮转，spec
§6 数据流 step 16/17、§7「调度与轮转」、§11 硬约束逐字）。

覆盖 Task 14 brief 的 5 个场景：

  - V10 主场景：造 9 个含 `COMPLETE` 的 `cass-*/`（generation 1-9）+ 1
    `SUSPECT-*` + 1 `INCOMPLETE-*` + 1 `RECOVERABLE-*` + 1 无 `COMPLETE` 的
    `cass-*/` + 1 `agent_search.db.pre-franken-*` 文件 → `KEEP=7` 跑一次成功
    备份（新发布 generation 10）→ 只剩 generation 4-10 的 7 个 `cass-*/`，
    其余每一项都原封（逐项断言）。
  - mtime 无关性：把最小 generation 的目录 `touch` 成 mtime 最新 → 仍按
    generation 被删（轮转排序不看 mtime）。
  - 读不到 generation 的 `cass-*/`（含 `COMPLETE` 但 `digest.json` 是坏 JSON）
    → 不参与轮转也不被删。
  - 轮转失败路径：`chmod 000` 某个待删目录 → 备份 exit 非零，但新发布的
    `cass-*/` 已完好（`ROTATE_FAIL` 构型，同 `backup-gbrain.sh`）。
  - 发布失败的晚上轮转不执行：用既有 `CASS_BACKUP_FAULT` 故障注入
    （`kill-after-db-backup`，在 step 15 发布之前就终止）验证轮转代码段
    根本没跑到——`DEST` 的 `cass-*/` 计数与名字逐一原封不动。

只有当前「链 tip」需要真实自洽的内容（`census.tsv` + `schema_fingerprint` /
`tables` / `meta_watermarks`）——本文件跑的那一晚真实 `backup-cass.sh` 会经
`cass_backup_gate.py` 读它做 leg3/4 历史比对。产出这份真内容用真五腿门 CLI
单独跑一次（`_publish_real_tip`），不整跑一遍 `backup-cass.sh`（省掉
doctor/sessions/blobs 的开销，逐字沿用 `test_rebaseline.py::_publish_baseline`
的既有模式）。其余陪衬的旧世代目录才是真正的轮转候选——轮转选点
（`cass_common.rotation_victims`）只读它们 `digest.json` 的 `generation`
字段，从不读内容，纯手工假货即可（`_make_fake_published`，`dumps_canonical`
序列化）。

本文件自包含，不跨文件 import 其它测试文件的私有函数（同代码库既有约定）。
大多数测试依赖真 `cass` 二进制构建 `synth_dd`（`requires_cass`）。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time

import pytest

import cass_common

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
GATE_SCRIPT = REPO / "infra" / "backup" / "cass" / "cass_backup_gate.py"

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd"
)


# ---------------------------------------------------------------------------
# 帮手（本文件自包含，不跨文件 import 其它测试文件的私有函数——同代码库既有约定）。
# ---------------------------------------------------------------------------


def _write_verified_doctor_stub(home: pathlib.Path, manifests_dir: pathlib.Path) -> None:
    """同其它 task 文件的写法——Tier 0 门必须先 PASS 才能走到 step 10+。"""
    import cass_manifest_census

    census, _ = cass_manifest_census.census_manifests(manifests_dir)
    summary = {
        "missing_blob_count": 0,
        "checksum_mismatch_count": 0,
        "manifest_checksum_mismatch_count": 0,
        "invalid_manifest_count": 0,
        "interrupted_capture_count": 0,
        "manifest_count": census.manifest_count,
        "verified_blob_count": census.unique_blobs,
        "duplicate_blob_reference_count": census.duplicate_refs,
    }
    doc = {"raw_mirror": {"status": "verified", "summary": summary}}
    (home / ".cass-stub-doctor.json").write_text(json.dumps(doc), encoding="utf-8")


def _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp, extra_env=None):
    """跑一次 backup-cass.sh，固定 stamp。首晚（`sessions.state.tsv` 缺失）默认给
    一份 ADOPT bootstrap env，让本文件的测试（关注轮转，不是 sessions 通道本身）
    不必逐个关心这道无关的门——同 `test_publish.py` 的 `_run`。"""
    _write_verified_doctor_stub(tmp_home, synth_dd / "raw-mirror" / "v1" / "manifests")
    env = {
        "CASS_DATA_DIR": str(synth_dd),
        "CASS_BACKUP_DEST": str(dest),
        "CASS_BACKUP_STAGING": str(staging),
        "CASS_BACKUP_STAMP": stamp,
        "CASS_BACKUP_ADOPT_SESSIONS": "1",
        "CASS_BACKUP_ADOPT_REASON": "test fixture — sessions channel not under test here",
        "PATH": f"{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    if extra_env:
        env.update(extra_env)
    rc, out, _dest = run_backup(env=env)
    return rc, out


def _run_gate_cli(db, dest, out_census, out_gate_json) -> tuple[int, str, str]:
    cmd = [
        str(VENV_PY), str(GATE_SCRIPT),
        "--db", str(db), "--dest", str(dest),
        "--out-census", str(out_census), "--out-gate-json", str(out_gate_json),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.returncode, result.stdout, result.stderr


def _publish_real_tip(dest, name, db, generation, scratch_dir) -> pathlib.Path:
    """跑一次真五腿门 CLI（不整跑一遍 `backup-cass.sh`，省掉 doctor/sessions/blobs
    的开销）产出真实 `census.tsv` + `schema_fingerprint`/`tables`/`meta_watermarks`，
    手工组装成一份「已发布」的 `cass-<name>/`（`COMPLETE` + `digest.json`，
    `generation` 改写成调用方指定值）。`scratch_dir` 是与 `dest` 无关的空目录，
    只为让 CLI 内部 `latest_published` 判定「无前驱」（first-backup 模式，
    leg3/4 不做历史比对）——传真 `dest` 会让它误读进已经手工塞进去的假目录
    （那些没有 `census.tsv`，会 FileNotFoundError）。"""
    scratch_dir = pathlib.Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    tip_census = scratch_dir / "census.tsv"
    tip_gate = scratch_dir / "gate.json"
    rc, out, err = _run_gate_cli(db, scratch_dir, tip_census, tip_gate)
    assert rc == 0, f"tip baseline 本身不应 FAIL：\nstdout={out}\nstderr={err}"

    dest = pathlib.Path(dest)
    backup_dir = dest / name
    backup_dir.mkdir()
    shutil.copy(tip_census, backup_dir / "census.tsv")
    gate = json.loads(tip_gate.read_bytes())
    digest = {
        "backup_name": name,
        "generation": generation,
        "prev_backup_name": "",
        "prev_sidecar_sha256": "",
        "db_sha256": "",
        "census_sha256": gate["census_sha256"],
        "sessions_tsv_sha256": "",
        "manifests_sha256sum_sha256": "",
        "schema_fingerprint": gate["schema_fingerprint"],
        "tables": gate["tables"],
        "meta_watermarks": gate["meta_watermarks"],
    }
    (backup_dir / "digest.json").write_bytes(cass_common.dumps_canonical(digest))
    (backup_dir / "COMPLETE").touch()
    return backup_dir


def _make_fake_published(dest, name, generation) -> pathlib.Path:
    """轮转候选的纯手工假货：只有 `COMPLETE` + 含 `generation` 的 `digest.json`——
    轮转选点（`cass_common.rotation_victims`）只读这一个字段，从不读内容。"""
    backup_dir = pathlib.Path(dest) / name
    backup_dir.mkdir()
    (backup_dir / "digest.json").write_bytes(
        cass_common.dumps_canonical({"generation": generation, "backup_name": name})
    )
    (backup_dir / "COMPLETE").touch()
    return backup_dir


def _make_protected_fixtures(dest: pathlib.Path) -> dict[str, pathlib.Path]:
    """spec §11 明确点名的、轮转绝不可删的非 `cass-*` 系列条目：`SUSPECT-*` /
    `INCOMPLETE-*` / `RECOVERABLE-*` / 既有 `agent_search.db.pre-franken-*`。
    （`raw-mirror/`/`sessions/`/`sessions.state.tsv` 由真实备份运行自然产生，
    见调用方测试里的断言——它们天然不匹配 `cass-*` glob，无需额外手工构造。）"""
    dest = pathlib.Path(dest)
    items: dict[str, pathlib.Path] = {}

    suspect = dest / "SUSPECT-oldstuff"
    suspect.mkdir()
    (suspect / "db").write_text("fake suspect db bytes", encoding="utf-8")
    items["suspect"] = suspect

    incomplete = dest / "INCOMPLETE-oldstuff"
    incomplete.mkdir()
    (incomplete / "db").write_text("fake incomplete db bytes", encoding="utf-8")
    items["incomplete"] = incomplete

    recoverable = dest / "RECOVERABLE-oldstuff"
    recoverable.mkdir()
    (recoverable / "COMPLETE").touch()
    items["recoverable"] = recoverable

    pre_franken = dest / "agent_search.db.pre-franken-0.1.9.20260704-1115"
    pre_franken.write_bytes(b"legacy manual snapshot bytes")
    items["pre_franken"] = pre_franken

    return items


def _assert_protected_fixtures_untouched(protected: dict[str, pathlib.Path]) -> None:
    assert (protected["suspect"] / "db").read_text(encoding="utf-8") == "fake suspect db bytes"
    assert (protected["incomplete"] / "db").read_text(encoding="utf-8") == "fake incomplete db bytes"
    assert (protected["recoverable"] / "COMPLETE").is_file()
    assert protected["pre_franken"].read_bytes() == b"legacy manual snapshot bytes"


# ---------------------------------------------------------------------------
# V10 主场景：轮转正确且范围受限
# ---------------------------------------------------------------------------


@requires_cass
def test_v10_keep7_rotation_by_generation_and_protected_set_untouched(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    db = synth_dd / "agent_search.db"
    scratch = tmp_path / "gate-scratch"
    _publish_real_tip(dest, "cass-fakegen-9", db, generation=9, scratch_dir=scratch)
    for g in range(1, 9):
        _make_fake_published(dest, f"cass-fakegen-{g}", generation=g)

    # 无 COMPLETE 的 cass-*/：即使 generation 号故意造得比所有人都大，也必须
    # 天然出局——轮转只匹配含 COMPLETE 的 cass-*/ 目录。
    no_complete = dest / "cass-nocomplete"
    no_complete.mkdir()
    (no_complete / "digest.json").write_bytes(cass_common.dumps_canonical({"generation": 99}))

    protected = _make_protected_fixtures(dest)

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v10-new",
        extra_env={"CASS_BACKUP_KEEP": "7"},
    )
    assert rc == 0, out

    new_dir = dest / "cass-v10-new"
    assert new_dir.is_dir(), out
    assert (new_dir / "COMPLETE").is_file()
    new_digest = json.loads((new_dir / "digest.json").read_bytes())
    assert new_digest["generation"] == 10, new_digest
    assert new_digest["prev_backup_name"] == "cass-fakegen-9", new_digest

    remaining = sorted(p.name for p in dest.glob("cass-*") if (p / "COMPLETE").is_file())
    expected = sorted([f"cass-fakegen-{g}" for g in range(4, 10)] + ["cass-v10-new"])
    assert remaining == expected, f"out={out}\nremaining={remaining}"

    for g in range(1, 4):
        assert not (dest / f"cass-fakegen-{g}").exists(), f"generation {g} 应已被轮转删除"

    assert no_complete.is_dir(), "无 COMPLETE 的 cass-*/ 不该被删"
    assert not (no_complete / "COMPLETE").exists()
    assert json.loads((no_complete / "digest.json").read_bytes())["generation"] == 99

    _assert_protected_fixtures_untouched(protected)

    assert (dest / "sessions").is_dir(), "sessions/ 通道自然产生，轮转不应触碰"
    assert (dest / "sessions.state.tsv").is_file(), "sessions.state.tsv 轮转不应触碰"
    assert (dest / "raw-mirror").is_dir(), "raw-mirror/（blob 池）轮转不应触碰"


# ---------------------------------------------------------------------------
# mtime 无关性：按 generation 排序，不按 mtime
# ---------------------------------------------------------------------------


@requires_cass
def test_rotation_ignores_mtime_orders_by_generation(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    db = synth_dd / "agent_search.db"
    scratch = tmp_path / "gate-scratch"
    gen1 = _make_fake_published(dest, "cass-mt-1", generation=1)
    _publish_real_tip(dest, "cass-mt-2", db, generation=2, scratch_dir=scratch)

    # generation 最小的目录 touch 成 mtime 最新——朴素按 mtime 排序的轮转会保留
    # 它、误删别的；按 generation 排序必须仍然选中它。
    future = time.time() + 10_000
    for p in (gen1, gen1 / "COMPLETE", gen1 / "digest.json"):
        os.utime(p, (future, future))

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "mt-new",
        extra_env={"CASS_BACKUP_KEEP": "2"},
    )
    assert rc == 0, out

    assert not gen1.exists(), "generation 最小者应被删——即使 mtime 摸成最新（不按 mtime 排序）"
    assert (dest / "cass-mt-2").is_dir(), "generation 2 应保留"
    new_dir = dest / "cass-mt-new"
    assert new_dir.is_dir() and (new_dir / "COMPLETE").is_file(), out


# ---------------------------------------------------------------------------
# 读不到 generation 的目录：不参与轮转也不被删
# ---------------------------------------------------------------------------


@requires_cass
def test_rotation_skips_dirs_with_unreadable_generation(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    db = synth_dd / "agent_search.db"
    scratch = tmp_path / "gate-scratch"
    gen1 = _make_fake_published(dest, "cass-bg-1", generation=1)
    _publish_real_tip(dest, "cass-bg-2", db, generation=2, scratch_dir=scratch)

    bad = dest / "cass-badgen"
    bad.mkdir()
    (bad / "COMPLETE").touch()
    bad_digest_bytes = b"{not valid json"
    (bad / "digest.json").write_bytes(bad_digest_bytes)

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "bg-new",
        extra_env={"CASS_BACKUP_KEEP": "2"},
    )
    assert rc == 0, out

    assert not gen1.exists(), "generation 1 应被轮转删除（bad digest 目录不计入 keep 名额）"
    assert (dest / "cass-bg-2").is_dir()
    assert (dest / "cass-bg-new").is_dir()

    assert bad.is_dir(), "读不到 generation 的目录不参与轮转，不应被删"
    assert (bad / "COMPLETE").is_file()
    assert (bad / "digest.json").read_bytes() == bad_digest_bytes, "坏 digest.json 内容应原封不动"


# ---------------------------------------------------------------------------
# 轮转失败路径：ROTATE_FAIL → loud（同 backup-gbrain.sh 构型）
# ---------------------------------------------------------------------------


@requires_cass
def test_rotation_delete_failure_is_loud_but_backup_stays_published(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    db = synth_dd / "agent_search.db"
    scratch = tmp_path / "gate-scratch"
    gen1 = _make_fake_published(dest, "cass-rf-1", generation=1)
    for g in range(2, 7):
        _make_fake_published(dest, f"cass-rf-{g}", generation=g)
    _publish_real_tip(dest, "cass-rf-7", db, generation=7, scratch_dir=scratch)

    # 0o555（r-x，无 w）而非 0o000：轮转选点仍需先*读到*这个目录的 generation
    # 才能正确把它选进待删名单——0o000 会让 digest.json 读取本身失败，那样它会
    # 被 `cass_common._iter_published` 当成「读不到 generation」直接跳过（既不
    # 参与也不被删），根本走不到 `rm -rf` 这一步。0o555 允许读、只挡写，精确
    # 复现「选中了、但删不掉」这一条 `ROTATE_FAIL` 路径。
    gen1.chmod(0o555)
    try:
        rc, out = _run(
            tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "rf-new",
            extra_env={"CASS_BACKUP_KEEP": "7"},
        )

        assert rc != 0, out
        assert "rotate rm failed" in out, out

        new_dir = dest / "cass-rf-new"
        assert new_dir.is_dir(), out
        assert (new_dir / "COMPLETE").is_file(), "备份本身已发布成功，不应因轮转失败回滚"

        assert gen1.is_dir(), "chmod 0o555 应让 rm -rf 删除失败，目录原地留守"
        assert (gen1 / "digest.json").is_file(), "只挡写不挡读——digest.json 应仍在原地"
        for g in range(2, 8):
            assert (dest / f"cass-rf-{g}").is_dir(), f"generation {g} 不应被误删（本轮唯一该删的是 generation 1）"
    finally:
        gen1.chmod(0o700)


# ---------------------------------------------------------------------------
# 发布失败的晚上：轮转代码段根本不执行
# ---------------------------------------------------------------------------


@requires_cass
def test_rotation_does_not_run_when_publish_fails(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    for g in range(1, 9):
        _make_fake_published(dest, f"cass-pf-{g}", generation=g)
    before = sorted(p.name for p in dest.glob("cass-*"))

    # kill-after-db-backup 发生在写锁段内、五腿门与 digest 组装之前——远早于
    # 脚本末尾的轮转代码段，也远早于任何 `dest` 读取。
    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "pf-fault",
        extra_env={
            "CASS_BACKUP_KEEP": "7",
            "CASS_BACKUP_FAULT": "kill-after-db-backup",
        },
    )
    assert rc != 0, out

    after = sorted(p.name for p in dest.glob("cass-*"))
    assert after == before, (
        "发布失败时轮转代码段不应执行，DEST 的 cass-*/ 计数与名字必须逐一原封不动："
        f"before={before} after={after}"
    )
