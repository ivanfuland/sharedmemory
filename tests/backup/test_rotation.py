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
    → 轮转选点仍宽容跳过（不参与轮转、不被删）；但整跑 backup-cass.sh 时五腿门
    在轮转前先跑 strict 基线选择（codex R4-P0），此成员让基线集不可信 → 门 FAIL、
    落 SUSPECT、轮转段不执行（rotation_victims 的宽容语义由 test_common.py 单测覆盖）。
  - 轮转失败路径：`chmod 0o555` 某个待删目录 → 备份 exit 非零，但新发布的
    `cass-*/` 已完好（`ROTATE_FAIL` 构型，同 `backup-gbrain.sh`）。
  - 发布失败的晚上轮转不执行：用既有 `CASS_BACKUP_FAULT` 故障注入
    （`kill-after-db-backup`，在 step 15 发布之前就终止）验证轮转代码段
    根本没跑到——`DEST` 的 `cass-*/` 计数与名字逐一原封不动。

外加 `_iter_published` 两层失败语义的钉子（codex review Task 14 修正轮）：

  - 目录探测层 loud：某 `cass-*/` 整体 `chmod 000` → `latest_published` 上抛
    `PermissionError`（单测）；其消费方五腿门 CLI exit 非零而非「首晚模式」
    假绿（e2e）。
  - digest 内容层 skip：`generation` 非 int（脏数据）→ 不参与轮转、不被删、
    不计入 keep 名额、也不让 `latest_published` 崩（单测）。
  - bash 侧「选点 python 崩 → ROTATE_FAIL」的 rc 分支**没有可从进程外构造的
    e2e 触发器**：能让选点崩的构造（目录 OS 级不可读）会先让 step 9 五腿门的
    `latest_published` 崩掉，当晚发布失败、根本走不到轮转段（正是上面 e2e 钉
    住的 loud 路径）；脏 generation 又落在 skip 语义内不会崩。该分支的正确性
    由「与脚本内其他 heredoc 相同的 rc 检查构型」+ 上述单测推理覆盖，
    诚实登记于 task-14-report.md。

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

import cass_chain
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


def _write_curl_stub(stub_dir: pathlib.Path) -> None:
    """PATH 上插一个永远 exit 0 的 curl stub——retention_reset/rebaseline 是审计
    事件，必须成功发 TG 才不置 TG_ALERT；测试环境没有真 TG，给个吞掉一切的 stub
    （同 test_publish.py 的写法）。"""
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "curl"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)


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


def _make_chained_published(dest, name, generation, prev_dir=None) -> pathlib.Path:
    """带**正确链指针**的最小「已发布」cass-*/（`COMPLETE` + digest.json，含
    `generation`/`prev_backup_name`/`prev_sidecar_sha256`）。`prev_dir=None` ⇒ 链头
    （prev 空）。用于「verify_chain 有意义」的轮转测试——`_make_fake_published`
    没有 prev 指针，verify_chain 会误报 fixture 自身的断链，没法区分「删除 bug 造成
    的断链」与「fixture 本来就没链」。"""
    backup_dir = pathlib.Path(dest) / name
    backup_dir.mkdir()
    if prev_dir is None:
        prev_name, prev_sha = "", ""
    else:
        prev_dir = pathlib.Path(prev_dir)
        prev_name = prev_dir.name
        prev_sha = cass_common.sha256_file(prev_dir / "digest.json")
    (backup_dir / "digest.json").write_bytes(
        cass_common.dumps_canonical(
            {
                "backup_name": name,
                "generation": generation,
                "prev_backup_name": prev_name,
                "prev_sidecar_sha256": prev_sha,
            }
        )
    )
    (backup_dir / "COMPLETE").touch()
    return backup_dir


def _chain_real_tip_to_prev(tip_dir, prev_dir) -> None:
    """把 `_publish_real_tip` 产出的真 tip 的 digest.json 补上指向 `prev_dir` 的链
    指针（census/tables/水位等真实字段不动——`_validate_baseline` 不看 prev 字段，
    故打补丁不破坏 gate 的基线校验/腿比对）。让真 tip 接到手工链尾巴上，使整条
    R 有完整指针、verify_chain 只可能因计数下界而非断链报问题。"""
    tip_dir = pathlib.Path(tip_dir)
    prev_dir = pathlib.Path(prev_dir)
    digest = json.loads((tip_dir / "digest.json").read_bytes())
    digest["prev_backup_name"] = prev_dir.name
    digest["prev_sidecar_sha256"] = cass_common.sha256_file(prev_dir / "digest.json")
    (tip_dir / "digest.json").write_bytes(cass_common.dumps_canonical(digest))


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
# 读不到 generation 的目录：轮转选点（rotation_victims）仍宽容跳过它（单测
# test_common.py::test_rotation_victims_still_lenient_skips_bad_generation 覆盖）；
# 但整跑 backup-cass.sh 时五腿门在轮转**之前**先跑 strict 基线选择（codex R4-P0），
# 真实 tip 不可读 = 基线集不可信 → 门 FAIL、落 SUSPECT、轮转段根本不执行。
# ---------------------------------------------------------------------------


@requires_cass
def test_corrupt_published_member_fails_gate_before_rotation_runs(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """codex R4-P0 交互（原 test_rotation_skips_dirs_with_unreadable_generation 在
    strict 基线选择下的正确新行为）：DEST 里放一个含 COMPLETE 但 digest.json 是坏
    JSON 的 cass-*/。轮转选点本身仍宽容跳过它（少删安全，见上方注释指向的单测），
    但整跑 backup-cass.sh 时五腿门先跑 strict 基线选择——含 COMPLETE 但 generation
    不可读的成员让基线集不可信 → 门 FAIL、落 SUSPECT、exit 非零，轮转段没跑到。
    因此谁也没被删（gen1 保留、坏目录原封、无新发布），比旧行为更保守安全。"""
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
    assert rc != 0, out
    assert "[baseline] FAIL" in out, out
    assert "cass-badgen" in out, out

    # 门 FAIL、轮转段没跑到 → 谁也没被删，没有新发布，落 SUSPECT。
    assert gen1.exists(), "门 FAIL、轮转未执行 → gen1 不应被删"
    assert (dest / "cass-bg-2").is_dir()
    assert not (dest / "cass-bg-new").exists(), "门 FAIL 当晚不发布"
    assert (dest / "SUSPECT-bg-new").is_dir(), out

    assert bad.is_dir(), "坏 digest 目录原封"
    assert (bad / "COMPLETE").is_file()
    assert (bad / "digest.json").read_bytes() == bad_digest_bytes, "坏 digest.json 内容应原封不动"


# ---------------------------------------------------------------------------
# codex R5-P1：retention_reset 轮转特殊语义（清掉所有 gen < 重置点的份，使重置点
# 成为链头）+ 同一次运行内 verify_chain preflight（不依赖 VERIFY_DOW）。
# ---------------------------------------------------------------------------


@requires_cass
def test_retention_reset_prunes_all_pre_reset_backups_and_chain_verifies(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """DEST 预置 3 份旧 cass-*（gen1-3，gen3 是真 tip 供 gate 做 leg3/4 比对 +
    R2/R4 基线校验）→ 带 retention_reset=1+reason 跑一晚 → 只剩新发布那份（旧 3
    份全被清，不是 keep-7 留超额），digest 含 retention_reset+reason，脚本内
    verify_chain preflight PASS（不依赖 VERIFY_DOW），独立再断言一次链 PASS。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    db = synth_dd / "agent_search.db"
    scratch = tmp_path / "gate-scratch"

    _make_fake_published(dest, "cass-old-1", generation=1)
    _make_fake_published(dest, "cass-old-2", generation=2)
    _publish_real_tip(dest, "cass-old-3", db, generation=3, scratch_dir=scratch)

    # retention_reset 是审计事件，成功也发 TG——给 curl stub + TG env，否则
    # TG_ALERT 会让脚本 exit 非零（那条分支由 test_publish.py 覆盖，这里聚焦轮转）。
    tg_env = tmp_path / "tg.env"
    tg_env.write_text(
        'TELEGRAM_BOT_TOKEN="fake-token"\nTELEGRAM_CHAT_ID="12345"\n', encoding="utf-8"
    )
    curl_stub_dir = tmp_path / "curl-stub-bin"
    _write_curl_stub(curl_stub_dir)

    reason = "manual retention window reset e2e"
    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "reset-new",
        extra_env={
            "CASS_BACKUP_RETENTION_RESET": "1",
            "CASS_BACKUP_RETENTION_RESET_REASON": reason,
            "CASS_BACKUP_KEEP": "7",
            "CASS_BACKUP_TG_ENV": str(tg_env),
            "PATH": f"{curl_stub_dir}{os.pathsep}{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    assert rc == 0, out

    new_dir = dest / "cass-reset-new"
    assert new_dir.is_dir(), out
    assert (new_dir / "COMPLETE").is_file()

    # 重置点之前一律清空（不是 keep-7 只删超额——4 份 < 7，keep-N 一个都不会删）：
    remaining = sorted(p.name for p in dest.glob("cass-*") if (p / "COMPLETE").is_file())
    assert remaining == ["cass-reset-new"], f"retention_reset 应清掉所有旧份：{remaining}\n{out}"
    for g in (1, 2, 3):
        assert not (dest / f"cass-old-{g}").exists(), f"cass-old-{g} 应被重置清掉"

    digest = json.loads((new_dir / "digest.json").read_bytes())
    assert digest["retention_reset"] is True
    assert digest["retention_reset_reason"] == reason

    # 脚本内 preflight 跑过且 PASS；这里独立再断言一次（R = {重置点}，合法链头）。
    assert "retention_reset chain preflight: PASS" in out, out
    assert cass_chain.verify_chain(dest, keep=7) == [], out


@requires_cass
def test_normal_night_keeps_pre_existing_backups_contrast(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """对照：同样预置 3 份旧 cass-*，但**不带** retention_reset 的普通晚 → keep-7
    下 4 份（旧 3 + 新 1）全部保留（旧份按 keep 保留，不被清），且不跑 preflight。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    db = synth_dd / "agent_search.db"
    scratch = tmp_path / "gate-scratch"

    _make_fake_published(dest, "cass-old-1", generation=1)
    _make_fake_published(dest, "cass-old-2", generation=2)
    _publish_real_tip(dest, "cass-old-3", db, generation=3, scratch_dir=scratch)

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "normal-new",
        extra_env={"CASS_BACKUP_KEEP": "7"},
    )
    assert rc == 0, out

    remaining = sorted(p.name for p in dest.glob("cass-*") if (p / "COMPLETE").is_file())
    assert remaining == ["cass-normal-new", "cass-old-1", "cass-old-2", "cass-old-3"], (
        f"普通晚 keep-7 下旧份应全部保留：{remaining}\n{out}"
    )
    assert "retention_reset chain preflight" not in out, "普通晚不该跑 retention_reset preflight"


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
    # 才能正确把它选进待删名单——0o000 会让目录探测（`COMPLETE` stat）直接
    # `PermissionError` 上抛，step 9 五腿门的 `latest_published` 当场崩、当晚
    # 发布失败，根本走不到轮转段（那条 loud 路径由
    # `test_unreadable_cass_dir_makes_gate_cli_loud_not_first_night` 钉住）。
    # 0o555 允许读、只挡写，精确复现「选中了、但删不掉」这一条 `ROTATE_FAIL`
    # 删除失败路径。
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
# codex R8-P1：多 victim 部分删除失败——升序 + 遇失败即 break ⇒ R 连续、链不断裂
# （旧「记 flag 继续删」会留 {g1,g4..} 断链）。
# ---------------------------------------------------------------------------


@requires_cass
def test_r8_rotation_multi_victim_delete_failure_breaks_keeps_chain_contiguous(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """codex 复现：真链 g1←…←g9（g9 真 tip 供 gate 比对）+ 发布 g10，KEEP=7 →
    victim=[g1,g2,g3]（升序）。g1（最老）chmod 555 使 rm 失败 → **break** 停止后续
    删除 → g2/g3 未被删（旧 continue 逻辑会删掉它们 → R={g1,g4..g10} 断链）。断言：
    全部 g1..g10 在（R 连续）、verify_chain **无断链/分叉问题**（只可能有良性的超额
    计数、下一轮自愈）、rc 非零。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    db = synth_dd / "agent_search.db"
    scratch = tmp_path / "gate-scratch"

    prev = _make_chained_published(dest, "cass-mv-1", 1, prev_dir=None)
    for g in range(2, 9):
        prev = _make_chained_published(dest, f"cass-mv-{g}", g, prev_dir=prev)
    tip = _publish_real_tip(dest, "cass-mv-9", db, generation=9, scratch_dir=scratch)
    _chain_real_tip_to_prev(tip, prev)  # g9.prev = g8，整条 R 指针完整

    g1 = dest / "cass-mv-1"
    g1.chmod(0o555)  # 最老 victim rm 失败
    try:
        rc, out = _run(
            tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "mv-10",
            extra_env={"CASS_BACKUP_KEEP": "7"},
        )
        assert rc != 0, out
        assert "halting further deletions" in out, out

        new_dir = dest / "cass-mv-10"
        assert new_dir.is_dir() and (new_dir / "COMPLETE").is_file(), "备份本身应发布成功"

        # break 后 R 连续：g1 删失败留守，g2/g3 因 break 未被删（关键区分点）。
        for g in range(1, 11):
            assert (dest / f"cass-mv-{g}").is_dir(), f"cass-mv-{g} 应仍在（R 连续，无缺口）"
        assert (dest / "cass-mv-2").is_dir() and (dest / "cass-mv-3").is_dir(), (
            "关键区分：break 保住了 g2/g3；旧 continue 逻辑会删掉它们造成 g4.prev 断链"
        )

        # verify_chain 无断链/分叉（只可能有超额计数下界问题，良性）：
        problems = cass_chain.verify_chain(dest, keep=7)
        assert not any(
            ("不在保留集内" in p) or ("孤儿" in p) or ("分叉" in p) for p in problems
        ), f"break 后 R 连续，不应有断链/分叉问题：{problems}"
    finally:
        g1.chmod(0o700)


@requires_cass
def test_r8_retention_reset_delete_failure_keeps_all_old_and_recovers(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """codex 同根复现（retention_reset 侧）：old1 删失败 → break → old2/old3 未删
    （旧 continue 会删掉它们 → old2/old3 丢失）。重置点因非链头被 preflight 判 FAIL
    → 进 RECOVERABLE（现有逻辑）→ 旧份零丢失。断言：old1/old2/old3 全留、重置点
    进 RECOVERABLE-*、无已发布 cass-reset-*、rc 非零。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    db = synth_dd / "agent_search.db"
    scratch = tmp_path / "gate-scratch"

    _make_fake_published(dest, "cass-old-1", generation=1)
    _make_fake_published(dest, "cass-old-2", generation=2)
    _publish_real_tip(dest, "cass-old-3", db, generation=3, scratch_dir=scratch)

    tg_env = tmp_path / "tg.env"
    tg_env.write_text(
        'TELEGRAM_BOT_TOKEN="fake-token"\nTELEGRAM_CHAT_ID="12345"\n', encoding="utf-8"
    )
    curl_stub_dir = tmp_path / "curl-stub-bin"
    _write_curl_stub(curl_stub_dir)

    old1 = dest / "cass-old-1"
    old1.chmod(0o555)  # 最老 victim rm 失败 → break
    try:
        rc, out = _run(
            tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "reset-new",
            extra_env={
                "CASS_BACKUP_RETENTION_RESET": "1",
                "CASS_BACKUP_RETENTION_RESET_REASON": "reset with delete failure",
                "CASS_BACKUP_KEEP": "7",
                "CASS_BACKUP_TG_ENV": str(tg_env),
                "PATH": f"{curl_stub_dir}{os.pathsep}{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
            },
        )
        assert rc != 0, out
        # old1 删失败 → break → old2/old3 未删；旧份零丢失。
        assert old1.is_dir(), out
        assert (dest / "cass-old-2").is_dir(), "break 后 old2 应保留（旧 continue 会删它 → 丢失）"
        assert (dest / "cass-old-3").is_dir(), "break 后 old3 应保留"
        # 重置点非链头（old1..3 仍在）→ preflight FAIL → 进 RECOVERABLE，不留作已发布 tip。
        assert (dest / "RECOVERABLE-reset-new").is_dir(), out
        assert not (dest / "cass-reset-new").exists(), "非链头的重置点不应留作已发布 cass-*"
        assert "preflight FAILED" in out, out
    finally:
        old1.chmod(0o700)


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


# ---------------------------------------------------------------------------
# _iter_published 两层失败语义的钉子（codex review Task 14 修正轮）
# ---------------------------------------------------------------------------


def test_latest_published_raises_on_unreadable_cass_dir_unit(tmp_path):
    """目录探测层 loud（单测）：某 `cass-*/` 整体 `chmod 000` → `latest_published`
    必须上抛 `PermissionError`，不得静默跳过——静默跳过会把「DEST 子目录权限坏」
    变成「首晚模式」（无前驱），绕过 generation 链比对。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    locked = _make_fake_published(dest, "cass-locked", generation=1)
    locked.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            cass_common.latest_published(dest)
        with pytest.raises(PermissionError):
            cass_common.rotation_victims(dest, 7)
    finally:
        locked.chmod(0o700)


@requires_cass
def test_unreadable_cass_dir_makes_gate_cli_loud_not_first_night(synth_dd, tmp_path):
    """目录探测层 loud（消费方 e2e）：`latest_published` 的基线解析消费方——五腿门
    CLI——遇到整体不可读的 `cass-*/` 必须 exit 非零（崩在基线解析），**而不是**
    把它当「首晚/无前驱」跑 first-backup 模式全腿 PASS、exit 0 发布假绿。
    （放宽版扫描（把探测层包进 except）在本场景下正是后者——reviewer 实测证伪，
    本测试钉死回退。）"""
    db = synth_dd / "agent_search.db"
    dest = tmp_path / "dest"
    dest.mkdir()
    locked = _make_fake_published(dest, "cass-locked", generation=1)
    locked.chmod(0o000)
    try:
        rc, out, err = _run_gate_cli(
            db, dest, tmp_path / "census.tsv", tmp_path / "gate.json"
        )
    finally:
        locked.chmod(0o700)

    assert rc != 0, f"必须响亮失败而非首晚假绿：stdout={out}\nstderr={err}"
    assert "PermissionError" in err or "Permission denied" in err, (
        f"失败原因必须是权限（loud 路径），不是别的环境错：stderr={err}"
    )


def test_rotation_victims_skips_non_int_generation_unit(tmp_path):
    """digest 内容层 skip（单测）：`generation` 非 int（脏数据，如手编 digest 把
    数字写成字符串）归入「读不到 generation」语义——不参与轮转、不被删、不计入
    keep 名额，也不让共享扫描在 sorted/max 的混型比较上 TypeError 崩掉。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    for g in range(1, 4):
        _make_fake_published(dest, f"cass-g{g}", generation=g)
    dirty = dest / "cass-dirty"
    dirty.mkdir()
    (dirty / "COMPLETE").touch()
    dirty_digest_bytes = cass_common.dumps_canonical({"generation": "2"})
    (dirty / "digest.json").write_bytes(dirty_digest_bytes)

    victims = cass_common.rotation_victims(dest, 2)
    assert victims == ["cass-g1"], (
        f"3 个合法候选、keep=2 → 只删 generation 1；脏 generation 不计入名额也不被选: {victims}"
    )

    tip = cass_common.latest_published(dest)
    assert tip is not None and tip[0] == "cass-g3", f"脏 generation 不得干扰 tip 判定: {tip}"
    assert (dirty / "digest.json").read_bytes() == dirty_digest_bytes, "脏目录原封不动"


def test_rotation_victims_skips_non_dict_digest_unit(tmp_path):
    """digest 内容层 skip（单测，whole-branch review 修复项）：`digest.json` 是
    合法 JSON 但裸标量（如 `5`）——`read_digest` 原样返回它，`"generation" not in
    digest` 对 int 会 TypeError。归入「读不到 generation」语义：不参与轮转、不
    被删、不计入 keep 名额，也不让共享扫描崩掉。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    for g in range(1, 4):
        _make_fake_published(dest, f"cass-g{g}", generation=g)
    dirty = dest / "cass-scalar"
    dirty.mkdir()
    (dirty / "COMPLETE").touch()
    dirty_digest_bytes = cass_common.dumps_canonical(5)
    (dirty / "digest.json").write_bytes(dirty_digest_bytes)

    victims = cass_common.rotation_victims(dest, 2)
    assert victims == ["cass-g1"], (
        f"3 个合法候选、keep=2 → 只删 generation 1；非 dict digest 不计入名额也不被选: {victims}"
    )

    tip = cass_common.latest_published(dest)
    assert tip is not None and tip[0] == "cass-g3", f"非 dict digest 不得干扰 tip 判定: {tip}"
    assert (dirty / "digest.json").read_bytes() == dirty_digest_bytes, "脏目录原封不动"
