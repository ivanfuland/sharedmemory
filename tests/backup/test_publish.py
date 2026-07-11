"""`infra/backup/backup-cass.sh` step 14c/15 的测试（Task 13：digest.json 组装 +
发布序列 + trap/RECOVERABLE + rebaseline/retention_reset TG，spec §6 数据流
step 14c, 15）。

覆盖 Task 13 brief 的 Step 1-3：

  - 健康全链 e2e（两晚）：digest.json 全字段齐、`generation`/`prev_backup_name`/
    `prev_sidecar_sha256` 链式推进正确，`db_sha256`/`census_sha256`/
    `sessions_tsv_sha256`/`manifests_sha256sum_sha256` 与同目录文件实算 sha256
    相等（V15e 正常路径）。
  - V7：`kill-after-db-backup`（step 7 后、step 10 前）→ NAS 零产物；
    `kill-after-incomplete-db-copy`（step 10 db 拷完后）→ 只有半成品
    `.incomplete-*/`，mtime 未满 1 天不清、老化后被下一轮清掉。
  - V7a：预造同名 `cass-<stamp>/`（旧内容）→ 发布前 `test ! -e` 拦下，旧目录
    原封不动，无嵌套 `.incomplete-*`。
  - V8：会话源根 `chmod 000` → sessions rsync 失败，落 `INCOMPLETE-*/`、无
    `cass-*/`，trap 不误删。
  - V15e：`kill-before-digest`（sessions.tsv/manifests.sha256sum 已落、digest
    未写）→ 目录无 `digest.json` 也无 `COMPLETE`（顺序契约）。
  - V15k：`kill-after-publish-mv`（`mv -T` 后、最终 sync/断言前）→ 已发布
    `cass-*/` 完好，下一轮陈旧 `.incomplete-*` 清理不误删它。
  - V15l：`kill-after-complete-marker`（`touch COMPLETE` 后、`mv -T` 前）→
    反例断言朴素按 mtime 清理的 glob 确实会命中它；老化后下一轮改名
    `RECOVERABLE-*` + exit 非零（DEV-6），且当晚自己的备份仍照常发布。
  - 14a e2e carry-forward（Task 10 遗留）：`corrupt-manifest-after-snapshot` →
    `manifests.sha256sum` 完整性门抓到调包，落 `INCOMPLETE-*/`，stdout 指认
    14a（此前 14a 的 FAIL 路径不可达）。
  - rebaseline 成功 TG（DEV-2）：PATH stub `curl` 记录调用参数——rebaseline
    成功发布后必须 curl 一次，text 含 reason 与被替换的基线名；`$CASS_BACKUP_
    TG_ENV` 缺失 → 备份仍已发布但 exit 非零。
  - retention_reset（DEV-3）：env 对齐跑一次 → digest.json 含
    `retention_reset:true` + `retention_reset_reason`（成对性拒绝已在 Task 9
    的 test_script_guards.py 覆盖，这里只补发布成功路径的 e2e 断言）。

SUSPECT 取证的 digest.json 升级（五腿门失败路径）断言在
`test_script_guards.py::test_five_leg_gate_failure_lands_suspect_with_forensics`
里（Task 9 的既有测试原地升级，未搬到本文件——同一份取证场景不应该在两个文件
里各建一次）。cass_sessions.py 的 `PROV adopt/self-heal/drift-fix` 留痕前缀
断言在 `test_sessions_state.py`（Task 12 的既有测试原地更新）。

大多数测试依赖真 `cass` 二进制构建 `synth_dd`（`requires_cass`，同其它 task
文件的约定）。本文件自包含，不跨文件 import 其它测试文件的私有函数。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import time

import pytest

import cass_common

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
SCRIPT = REPO / "infra" / "backup" / "backup-cass.sh"

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
    一份 ADOPT bootstrap env，让本文件的测试（关注 digest/发布序列，不是 sessions
    通道本身）不必逐个关心这道无关的门——同 test_blobs_manifests.py 的 `_run`。"""
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


def _migrate_agents_schema_text(db_path) -> None:
    """模拟一次合法 schema 迁移（如 `ALTER TABLE agents ADD COLUMN x`）对 `agents`
    DDL 文本的影响，直接改写 `sqlite_master.sql`（`PRAGMA writable_schema`）。逐字
    搬自 `test_leg34_gate.py`/`test_rebaseline.py` 的同名 helper（brief 明确允许
    复制）——真 `ALTER TABLE` 在这份 synth_dd 文件上会稳定触发一个与本模块无关的
    SQLite 内部 bug，直接改写 DDL 文本能精确达到「schema 变了」这一测试目的且不
    依赖那条不稳定路径。"""
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


def _write_curl_stub(stub_dir: pathlib.Path, calls_log: pathlib.Path) -> None:
    """PATH 上插一个记录调用参数（逐个 argv，一行一个）的 curl stub，永远 exit 0。"""
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{calls_log}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


# ---------------------------------------------------------------------------
# 健康全链 e2e：两晚 digest.json 全字段 + generation 链式推进 + sha 字段自洽
# ---------------------------------------------------------------------------


@requires_cass
def test_healthy_two_night_chain_digest_fields_and_generation(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "night1")
    assert rc1 == 0, out1

    night1_dir = dest / "cass-night1"
    assert night1_dir.is_dir(), out1
    assert (night1_dir / "COMPLETE").is_file()
    assert (night1_dir / "db").is_file()
    assert (night1_dir / "db.sha256").is_file()
    assert (night1_dir / "census.tsv").is_file()
    assert (night1_dir / "sessions.tsv").is_file()
    assert (night1_dir / "manifests.sha256sum").is_file()
    assert (night1_dir / "manifests").is_dir()
    assert (night1_dir / "digest.json").is_file()

    digest1 = json.loads((night1_dir / "digest.json").read_bytes())
    for key in (
        "backup_name", "generation", "prev_backup_name", "prev_sidecar_sha256",
        "db_sha256", "census_sha256", "sessions_tsv_sha256",
        "manifests_sha256sum_sha256", "schema_fingerprint", "tables",
        "meta_watermarks",
    ):
        assert key in digest1, f"digest.json 缺字段 {key}: {digest1}"
    assert digest1["backup_name"] == "cass-night1"
    assert digest1["generation"] == 1
    assert digest1["prev_backup_name"] == ""
    assert digest1["prev_sidecar_sha256"] == ""
    assert digest1["db_sha256"] == cass_common.sha256_file(night1_dir / "db")
    assert digest1["census_sha256"] == cass_common.sha256_file(night1_dir / "census.tsv")
    assert digest1["sessions_tsv_sha256"] == cass_common.sha256_file(night1_dir / "sessions.tsv")
    assert digest1["manifests_sha256sum_sha256"] == cass_common.sha256_file(
        night1_dir / "manifests.sha256sum"
    )

    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "night2")
    assert rc2 == 0, out2

    night2_dir = dest / "cass-night2"
    assert night2_dir.is_dir(), out2
    digest2 = json.loads((night2_dir / "digest.json").read_bytes())
    assert digest2["backup_name"] == "cass-night2"
    assert digest2["generation"] == 2
    assert digest2["prev_backup_name"] == "cass-night1"
    assert digest2["prev_sidecar_sha256"] == cass_common.sha256_file(night1_dir / "digest.json")
    assert digest2["db_sha256"] == cass_common.sha256_file(night2_dir / "db")
    assert digest2["census_sha256"] == cass_common.sha256_file(night2_dir / "census.tsv")
    assert digest2["sessions_tsv_sha256"] == cass_common.sha256_file(night2_dir / "sessions.tsv")
    assert digest2["manifests_sha256sum_sha256"] == cass_common.sha256_file(
        night2_dir / "manifests.sha256sum"
    )


# ---------------------------------------------------------------------------
# V7：两个 DEV-7 注入点分开测（codex R3-P2 澄清）
# ---------------------------------------------------------------------------


@requires_cass
def test_v7_kill_after_db_backup_zero_nas_artifacts(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """`.backup` 刚完成、`.incomplete-*` 尚未创建就 SIGKILL——此刻还没有任何 NAS
    写入，DEST 必须零产物。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v7-backup",
        extra_env={"CASS_BACKUP_FAULT": "kill-after-db-backup"},
    )
    assert rc != 0, out
    assert list(dest.iterdir()) == [], f"kill-after-db-backup 必须零 NAS 产物: {list(dest.iterdir())}"


@requires_cass
def test_v7_kill_after_incomplete_db_copy_stale_cleanup_lifecycle(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """db 刚拷进 `.incomplete-$STAMP` 就 SIGKILL——留下一个没有 COMPLETE 的半成品。
    mtime 未满 1 天时下一轮不清；`touch -d '2 days ago'` 老化后被当垃圾清掉。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v7-copy",
        extra_env={"CASS_BACKUP_FAULT": "kill-after-incomplete-db-copy"},
    )
    assert rc != 0, out

    incomplete = dest / ".incomplete-v7-copy"
    assert incomplete.is_dir(), out
    assert not (incomplete / "COMPLETE").exists()
    assert not list(dest.glob("cass-*")), out
    assert not list(dest.glob("INCOMPLETE-*")), (
        "SIGKILL 绕过 trap，半成品应原地留守，不应被改名"
    )

    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v7-copy-second")
    assert rc2 == 0, out2
    assert incomplete.is_dir(), "mtime 未满 1 天不应被下一轮清理"

    two_days_ago = time.time() - 2 * 86400
    os.utime(incomplete, (two_days_ago, two_days_ago))
    rc3, out3 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v7-copy-third")
    assert rc3 == 0, out3
    assert not incomplete.exists(), "老化后应被下一轮当垃圾清掉（无 COMPLETE 的半成品）"


# ---------------------------------------------------------------------------
# V7a：发布目标已存在——发布前 test ! -e 拦下，旧内容原封不动，无嵌套 .incomplete。
#
# codex R2-P2 修复：这个失败点在 `touch COMPLETE`（step 15）**之后**——载荷已全量
# 校验通过，必须改名 `RECOVERABLE-<stamp>`（含 COMPLETE），不能像修复前那样改名
# `INCOMPLETE-<stamp>`（INCOMPLETE-* 内永不含 COMPLETE 是不变式，spec §6.6：
# 「含 COMPLETE 的载荷」定义为 RECOVERABLE 状态）。
# ---------------------------------------------------------------------------


@requires_cass
def test_v7a_publish_target_already_exists_refuses_and_preserves_old_content(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    stamp = "v7a-collide"

    old_dir = dest / f"cass-{stamp}"
    old_dir.mkdir()
    (old_dir / "sentinel.txt").write_text("pre-existing unrelated content\n", encoding="utf-8")

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp)

    assert rc != 0, out
    assert (old_dir / "sentinel.txt").read_text(encoding="utf-8") == (
        "pre-existing unrelated content\n"
    ), "旧目录内容必须原封不动（顶层 cass-* 未变）"
    assert not (old_dir / "COMPLETE").exists(), "旧目录不该被本轮内容污染"
    assert not (dest / f".incomplete-{stamp}").exists(), "不应留嵌套 .incomplete-*"
    assert not (dest / f"INCOMPLETE-{stamp}").exists(), (
        "touch COMPLETE 之后的失败必须是 RECOVERABLE，不是 INCOMPLETE（spec §6.6）: " + out
    )
    recoverable_dir = dest / f"RECOVERABLE-{stamp}"
    assert recoverable_dir.is_dir(), out
    assert (recoverable_dir / "COMPLETE").is_file(), "载荷已全量校验通过，COMPLETE 必须随行"
    assert not (recoverable_dir / f".incomplete-{stamp}").exists(), "不应有嵌套 .incomplete-*"
    assert not (recoverable_dir / stamp).exists(), "不应有嵌套目录"
    assert "already exists" in out, out


# ---------------------------------------------------------------------------
# V8：sessions rsync 失败（源根 chmod 000）→ 只有 INCOMPLETE-*、无 cass-*
# ---------------------------------------------------------------------------


@requires_cass
def test_v8_sessions_rsync_failure_lands_incomplete_not_cass(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    stamp = "v8-perm"
    blocked_root = tmp_home / ".claude" / "projects"
    blocked_root.chmod(0o000)
    try:
        rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp)
    finally:
        blocked_root.chmod(0o700)

    assert rc != 0, out
    assert (dest / f"INCOMPLETE-{stamp}").is_dir(), out
    assert not (dest / f"INCOMPLETE-{stamp}" / "COMPLETE").exists(), (
        "INCOMPLETE-* 内永无 COMPLETE（spec §6.6 不变式，codex R2-P2 回归线）"
    )
    assert not (dest / f".incomplete-{stamp}").exists(), out
    assert not list(dest.glob("cass-*")), out
    assert "sessions rsync failed" in out, out


# ---------------------------------------------------------------------------
# V15e：顺序契约——sessions.tsv/manifests.sha256sum 已落、digest.json 写入前 kill
# ---------------------------------------------------------------------------


@requires_cass
def test_v15e_kill_before_digest_no_digest_no_complete(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    stamp = "v15e-digest"

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp,
        extra_env={"CASS_BACKUP_FAULT": "kill-before-digest"},
    )
    assert rc != 0, out

    incomplete = dest / f".incomplete-{stamp}"
    assert incomplete.is_dir(), out
    assert (incomplete / "sessions.tsv").is_file(), "13g 必须已经落地"
    assert (incomplete / "manifests.sha256sum").is_file(), "step 10 必须已经落地"
    assert not (incomplete / "digest.json").exists(), "kill 点在 digest.json 写入前"
    assert not (incomplete / "COMPLETE").exists()
    assert not list(dest.glob("cass-*")), out


# ---------------------------------------------------------------------------
# V15k：mv -T 后、sync/最终断言前 kill——已发布目录完好，下一轮清理不误删
# ---------------------------------------------------------------------------


@requires_cass
def test_v15k_kill_after_publish_mv_published_dir_intact_next_cleanup_safe(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    stamp = "v15k-mv"

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp,
        extra_env={"CASS_BACKUP_FAULT": "kill-after-publish-mv"},
    )
    assert rc != 0, out  # SIGKILL：非正常终止

    published = dest / f"cass-{stamp}"
    assert published.is_dir(), out
    assert (published / "COMPLETE").is_file()
    assert (published / "digest.json").is_file()
    assert not (dest / f".incomplete-{stamp}").exists()

    # 老化它（模拟隔了一晚），再跑一轮——下一轮陈旧 .incomplete-* 清理找不到
    # 同名目标，不会误删已发布的 cass-*/。
    two_days_ago = time.time() - 2 * 86400
    os.utime(published, (two_days_ago, two_days_ago))
    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v15k-second")
    assert rc2 == 0, out2
    assert published.is_dir(), "已发布 cass-*/ 不应被下一轮清理误删"
    assert (published / "COMPLETE").is_file()


# ---------------------------------------------------------------------------
# V15l：touch COMPLETE 后、mv -T 前 kill——RECOVERABLE 救援 + DEV-6 当晚照常发布
# ---------------------------------------------------------------------------


@requires_cass
def test_v15l_kill_after_complete_marker_becomes_recoverable_next_run(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    stamp = "v15l-marker"

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp,
        extra_env={"CASS_BACKUP_FAULT": "kill-after-complete-marker"},
    )
    assert rc != 0, out  # SIGKILL

    incomplete = dest / f".incomplete-{stamp}"
    assert incomplete.is_dir(), out
    assert (incomplete / "COMPLETE").is_file(), "kill 点在 touch COMPLETE 之后"
    assert (incomplete / "digest.json").is_file(), "digest.json 在 COMPLETE 之前落盘"

    two_days_ago = time.time() - 2 * 86400
    os.utime(incomplete, (two_days_ago, two_days_ago))

    # 反例：朴素「按 mtime 删超 1 天 .incomplete-*」的 glob 确实会命中它——不做
    # COMPLETE 特判就会把一份完整、已全部校验通过的备份当垃圾删掉（这正是 step 4
    # 那段 COMPLETE 特判存在的理由）。
    naive_stale_matches = [
        d for d in dest.glob(".incomplete-*")
        if d.is_dir() and (time.time() - d.stat().st_mtime) > 86400
    ]
    assert incomplete in naive_stale_matches, "反例夹具自检：朴素 glob 应该命中它"

    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v15l-second")

    assert rc2 != 0, out2  # ALERT_FLAG：即使当晚备份也发布成功，仍要 exit 非零
    recoverable = dest / f"RECOVERABLE-{stamp}"
    assert recoverable.is_dir(), out2
    assert (recoverable / "COMPLETE").is_file()
    assert not incomplete.exists(), "原 .incomplete-* 应已被改名走"
    assert "RECOVERABLE" in out2, out2

    published_second = dest / "cass-v15l-second"
    assert published_second.is_dir(), "DEV-6：当晚备份应照常继续发布"
    assert (published_second / "COMPLETE").is_file()


# ---------------------------------------------------------------------------
# codex R3-P1：touch COMPLETE 之后的裸失败（sync 被 set -e 打出）——EXIT trap
# 必须自己认 COMPLETE，把已全量校验的载荷保成 RECOVERABLE-*，绝不 rm -rf。
# 修复前复现：fake-sync 第二次失败 → set -e 打出 → 走不到 fail_recoverable →
# trap 仍持 TRAP_INCOMPLETE → rm -rf 把完整备份静默丢弃（DEST 只剩 raw-mirror/
# sessions/state，.incomplete/INCOMPLETE/RECOVERABLE/cass 一个都没有）。
# ---------------------------------------------------------------------------


@requires_cass
def test_r3_sync_failure_after_complete_trap_preserves_recoverable(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """`CASS_BACKUP_FAULT=fail-sync-after-complete`：`touch COMPLETE` 后注入
    `false`（与真 sync 失败同一条 set -e 退出路径，绕过 fail_recoverable）→
    EXIT trap 兜底：载荷改名 `RECOVERABLE-<stamp>`（含 COMPLETE + 完整载荷），
    exit 非零，无裸删、无 INCOMPLETE-*、无发布。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    stamp = "r3-sync"

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp,
        extra_env={"CASS_BACKUP_FAULT": "fail-sync-after-complete"},
    )

    assert rc != 0, out
    recoverable = dest / f"RECOVERABLE-{stamp}"
    assert recoverable.is_dir(), f"trap 必须把 COMPLETE 载荷保成 RECOVERABLE，不能裸删: {out}"
    assert (recoverable / "COMPLETE").is_file()
    # 载荷完整（已全量校验过的那一份，一件不少）：
    for artifact in ("db", "db.sha256", "census.tsv", "sessions.tsv",
                     "manifests.sha256sum", "digest.json"):
        assert (recoverable / artifact).is_file(), f"载荷缺 {artifact}: {out}"
    assert (recoverable / "manifests").is_dir()
    assert not (dest / f".incomplete-{stamp}").exists(), out
    assert not (dest / f"INCOMPLETE-{stamp}").exists(), (
        "COMPLETE 载荷绝不能变成 INCOMPLETE-*（spec §6.6 不变式）"
    )
    assert not list(dest.glob("cass-*")), f"sync 失败当晚不得发布: {out}"
    assert "EXIT trap found COMPLETE payload" in out, out


@requires_cass
def test_r3_success_path_trap_does_not_touch_published(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """对照回归：正常成功路径 `TRAP_INCOMPLETE` 在发布后已清空——EXIT trap 不碰
    已发布的 `cass-*/`，也不凭空产出 RECOVERABLE-*/INCOMPLETE-*。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    stamp = "r3-ok"

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp)

    assert rc == 0, out
    published = dest / f"cass-{stamp}"
    assert published.is_dir(), out
    assert (published / "COMPLETE").is_file()
    assert (published / "digest.json").is_file()
    assert not list(dest.glob("RECOVERABLE-*")), out
    assert not list(dest.glob("INCOMPLETE-*")), out
    assert not list(dest.glob(".incomplete-*")), out
    assert "EXIT trap found COMPLETE payload" not in out, out


# ---------------------------------------------------------------------------
# 14a e2e carry-forward（Task 10 遗留）：corrupt-manifest-after-snapshot
# ---------------------------------------------------------------------------


@requires_cass
def test_14a_corrupt_manifest_after_snapshot_fails_sha256sum_gate(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    stamp = "14a-corrupt"

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp,
        extra_env={"CASS_BACKUP_FAULT": "corrupt-manifest-after-snapshot"},
    )

    assert rc != 0, out
    assert (dest / f"INCOMPLETE-{stamp}").is_dir(), out
    assert not (dest / f"INCOMPLETE-{stamp}" / "COMPLETE").exists(), (
        "INCOMPLETE-* 内永无 COMPLETE（spec §6.6 不变式，codex R2-P2 回归线）"
    )
    assert not (dest / f".incomplete-{stamp}").exists(), out
    assert not list(dest.glob("cass-*")), out
    assert "step 14a" in out, out


# ---------------------------------------------------------------------------
# rebaseline 成功 TG（DEV-2）
# ---------------------------------------------------------------------------


@requires_cass
def test_rebaseline_success_sends_tg_with_reason_and_baseline_name(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "rb-baseline")
    assert rc1 == 0, out1

    _migrate_agents_schema_text(synth_dd / "agent_search.db")

    tg_env = tmp_path / "tg.env"
    tg_env.write_text(
        'TELEGRAM_BOT_TOKEN="fake-token"\nTELEGRAM_CHAT_ID="12345"\n', encoding="utf-8"
    )
    curl_calls = tmp_path / "curl_calls.log"
    curl_stub_dir = tmp_path / "curl-stub-bin"
    _write_curl_stub(curl_stub_dir, curl_calls)

    reason = "CASS 0.6.18 迁移，schema_version 20→21"
    rc2, out2 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "rb-migrated",
        extra_env={
            "CASS_BACKUP_REBASELINE": "cass-rb-baseline",
            "CASS_BACKUP_REBASELINE_REASON": reason,
            "CASS_BACKUP_TG_ENV": str(tg_env),
            "PATH": f"{curl_stub_dir}{os.pathsep}{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    assert rc2 == 0, out2

    migrated_dir = dest / "cass-rb-migrated"
    assert migrated_dir.is_dir(), out2
    digest2 = json.loads((migrated_dir / "digest.json").read_bytes())
    assert digest2["rebaselined_from"] == "cass-rb-baseline"
    assert digest2["reason"] == reason

    assert curl_calls.is_file(), f"curl stub 必须被调用一次: {out2}"
    call_text = curl_calls.read_text(encoding="utf-8")
    assert "cass-rb-baseline" in call_text, call_text
    assert reason in call_text, call_text


@requires_cass
def test_rebaseline_success_tg_default_env_path_reachable(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """回归钉死（review Important）：**不设** `CASS_BACKUP_TG_ENV` 时，脚本必须
    source 的是脚本开头算好的默认路径 `$HOME/.claude/channels/telegram/.env`
    ——这正是 §5.7 的主用例（人工 rebaseline 通常不会带这个 env）。修复前 TG
    子 shell source 的是裸 `$CASS_BACKUP_TG_ENV`（空串 no-op）→ token/chat_id
    展开为空 → curl 打 404 → 假 TG_ALERT，默认路径的审计消息永远送不出。上下
    两个 TG 测试都显式传 env，遮蔽了这条回归线。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "rb3-baseline")
    assert rc1 == 0, out1

    _migrate_agents_schema_text(synth_dd / "agent_search.db")

    # env 文件放在 tmp HOME 的**默认**路径（不传 CASS_BACKUP_TG_ENV）。
    default_tg_env = tmp_home / ".claude" / "channels" / "telegram" / ".env"
    default_tg_env.parent.mkdir(parents=True, exist_ok=True)
    default_tg_env.write_text(
        'TELEGRAM_BOT_TOKEN="fake-token"\nTELEGRAM_CHAT_ID="12345"\n', encoding="utf-8"
    )
    curl_calls = tmp_path / "curl_calls.log"
    curl_stub_dir = tmp_path / "curl-stub-bin"
    _write_curl_stub(curl_stub_dir, curl_calls)

    reason = "default TG env path regression test"
    rc2, out2 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "rb3-migrated",
        extra_env={
            "CASS_BACKUP_REBASELINE": "cass-rb3-baseline",
            "CASS_BACKUP_REBASELINE_REASON": reason,
            # 故意不传 CASS_BACKUP_TG_ENV——走默认路径
            "PATH": f"{curl_stub_dir}{os.pathsep}{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    assert rc2 == 0, out2
    assert (dest / "cass-rb3-migrated" / "COMPLETE").is_file(), out2

    assert curl_calls.is_file(), f"默认路径的 TG env 必须可达（curl stub 必须被调用）: {out2}"
    call_text = curl_calls.read_text(encoding="utf-8")
    assert "fake-token" in call_text, "token 必须来自默认路径的 env 文件"
    assert "cass-rb3-baseline" in call_text, call_text
    assert reason in call_text, call_text


@requires_cass
def test_rebaseline_success_but_tg_env_missing_still_published_but_nonzero(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "rb2-baseline")
    assert rc1 == 0, out1

    _migrate_agents_schema_text(synth_dd / "agent_search.db")

    missing_tg_env = tmp_path / "does-not-exist.env"

    rc2, out2 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "rb2-migrated",
        extra_env={
            "CASS_BACKUP_REBASELINE": "cass-rb2-baseline",
            "CASS_BACKUP_REBASELINE_REASON": "TG env missing regression test",
            "CASS_BACKUP_TG_ENV": str(missing_tg_env),
        },
    )

    assert rc2 != 0, out2
    migrated_dir = dest / "cass-rb2-migrated"
    assert migrated_dir.is_dir(), out2
    assert (migrated_dir / "COMPLETE").is_file(), "备份本身必须已发布，不回滚"
    digest2 = json.loads((migrated_dir / "digest.json").read_bytes())
    assert digest2["rebaselined_from"] == "cass-rb2-baseline"
    assert "TG" in out2, out2


# ---------------------------------------------------------------------------
# retention_reset（DEV-3）：成对性拒绝已在 test_script_guards.py 覆盖，这里补
# 发布成功路径的 e2e 断言（digest.json 含标志与 reason）。
# ---------------------------------------------------------------------------


@requires_cass
def test_retention_reset_pair_set_digest_has_flag_and_reason(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """retention_reset 与 rebaseline 同为「即使成功也发 TG」的人工审计事件（brief
    「retention_reset 同样发」）——给一份能工作的 curl stub + TG env，把断言焦点
    留在 digest.json 的字段上，TG 发送本身的成功/失败分支已在 rebaseline TG 的
    两个测试里覆盖过。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    reason = "e2e regression: manual retention window reset"

    tg_env = tmp_path / "tg.env"
    tg_env.write_text(
        'TELEGRAM_BOT_TOKEN="fake-token"\nTELEGRAM_CHAT_ID="12345"\n', encoding="utf-8"
    )
    curl_calls = tmp_path / "curl_calls.log"
    curl_stub_dir = tmp_path / "curl-stub-bin"
    _write_curl_stub(curl_stub_dir, curl_calls)

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "reset-ok",
        extra_env={
            "CASS_BACKUP_RETENTION_RESET": "1",
            "CASS_BACKUP_RETENTION_RESET_REASON": reason,
            "CASS_BACKUP_TG_ENV": str(tg_env),
            "PATH": f"{curl_stub_dir}{os.pathsep}{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    assert rc == 0, out
    digest = json.loads((dest / "cass-reset-ok" / "digest.json").read_bytes())
    assert digest["retention_reset"] is True
    assert digest["retention_reset_reason"] == reason

    call_text = curl_calls.read_text(encoding="utf-8")
    assert "retention_reset" in call_text, call_text
    assert reason in call_text, call_text
