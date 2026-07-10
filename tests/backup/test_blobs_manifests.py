"""`infra/backup/backup-cass.sh` step 10-12/14a/14b 的测试（Task 10：`.incomplete` 布局 /
O_DIRECT 读回 / blobs 池 / manifests 双门，spec §6 数据流 step 10-12, 14a, 14b）。

覆盖 Task 10 brief 的 Step 1-3：

  - V9：`CASS_BACKUP_FAULT=flip-nas-db` / `unlink-nas-db-before-readback` 两个测试专用
    故障注入点（DEV-7）→ O_DIRECT 读回抓出 staging(A) 与 NAS(B) 不一致 → `INCOMPLETE-*`
    落地（证明走的是 `fail_incomplete` 而非被 `set -e` 在 RC 捕获前击杀）；外加 PIPESTATUS
    的 bash -c 反例演示。
  - V11：blobs 池共享——第二次备份 rsync 传输 0 文件；`raw-mirror/v1/tmp/` 不进 NAS。
  - V13 系列：manifests 随每份备份走且不可变（V13）；`manifests.sha256sum` 是发布门、
    `sha256sum -c` 能查出内容篡改而 `rsync -a` quick-check 会跳过（V13a）；14b 四项过而
    14a 靠 `manifests.sha256sum` 单独抓出整体调包（V13a2，unit-level——TOCTOU 场景无法
    从进程外构造，本 task 的 `CASS_BACKUP_FAULT` 枚举也不含此钩子）；发布前闭合检查的
    存在性 / `st_size` / BLAKE3 内容三道判据（V13b/V13c/V13c2）；`blob_relative_path`
    路径穿越只做形状校验、绝不参与文件系统操作（V13d）。
  - `--publish-check` 子命令的若干单元测试。

大多数测试依赖真 `cass` 二进制构建 `synth_dd`（`requires_cass`，同 test_script_guards.py
的约定）。V13a2/V13b 的核心判据是纯 Python 单元测试，不需要走完整脚本。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import blake3
import pytest

import cass_manifest_census

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
CENSUS_SCRIPT = REPO / "infra" / "backup" / "cass" / "cass_manifest_census.py"

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd"
)


# ---------------------------------------------------------------------------
# 帮手（本文件自包含，不跨文件 import test_script_guards.py 的私有函数——同代码库
# 既有约定：每个测试文件各自持一份小工具）。
# ---------------------------------------------------------------------------


def _write_verified_doctor_stub(home: pathlib.Path, manifests_dir: pathlib.Path) -> None:
    """按 manifests_dir 的真实普查数造一份「与 census 恒等式吻合」的 doctor stub JSON
    ——Tier 0 门必须先 PASS 才能走到本 task 覆盖的 step 10+。"""
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
    """跑一次 backup-cass.sh，固定 stamp 供测试按名字定位产物目录。

    Task 12 起，首晚（`sessions.state.tsv` 缺失）需要显式 ADOPT（spec §6.3.1 step
    13a）——本文件的测试全部关注 db/blob/manifest 通道，与 sessions 通道正交，
    默认给一份 ADOPT env 让它们不必逐个关心这道无关的门（同 `_write_verified_
    doctor_stub` 替这些测试挡掉 Tier 0 门的思路一致）。`extra_env` 仍可覆盖。"""
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


def _mutate_manifest_db_links(manifest_path: pathlib.Path) -> None:
    """源端合法改写：往 db_links 追加一条记录 + 重算 manifest_blake3（占位算法——
    生产是 cass 内部哈希，这里只需要「内容确实变了」，不需要复刻其精确算法）。"""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.setdefault("db_links", []).append(
        {
            "conversation_id": 999,
            "message_count": 1,
            "source_path": "/synthetic/mutated-by-test.jsonl",
            "started_at_ms": 0,
        }
    )
    data["manifest_blake3"] = "doctor-raw-mirror-manifest-v1-" + blake3.blake3(
        json.dumps(data, sort_keys=True).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 1 — V9: DEV-7 故障注入两点 + PIPESTATUS 反例演示
# ---------------------------------------------------------------------------


@requires_cass
def test_v9_flip_nas_db_lands_incomplete_not_bare_exit(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """`CASS_BACKUP_FAULT=flip-nas-db` 在 step 10 拷完后翻转 NAS 副本一个字节——
    O_DIRECT 读回必须抓出 staging(A) != NAS(B)，落 `INCOMPLETE-*` 而非裸退。
    `INCOMPLETE-*` 的存在同时证明失败走的是 `fail_incomplete` 路径而非被 `set -e`
    在 RC 捕获前击杀（codex R2-P0 回归判据：若被裸杀，EXIT trap 会把
    `.incomplete-*` 直接 rm -rf，两个名字都不会剩下）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    stamp = "v9-flip"

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp,
        extra_env={"CASS_BACKUP_FAULT": "flip-nas-db"},
    )

    assert rc != 0, out
    assert (dest / f"INCOMPLETE-{stamp}").is_dir(), out
    assert not (dest / f".incomplete-{stamp}").exists(), (
        f"fail_incomplete 必须把半成品改名走，不能同时留 .incomplete-* 和 INCOMPLETE-*: {out}"
    )
    assert not list(dest.glob("cass-*")), out
    assert "readback" in out.lower(), out


@requires_cass
def test_v9_unlink_nas_db_before_readback_lands_incomplete(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """`CASS_BACKUP_FAULT=unlink-nas-db-before-readback`：step 11 前删掉 NAS 副本的
    db 文件——dd 读一个不存在的文件必须仍然落 `INCOMPLETE-*`，而不是让 `dd | sha256sum`
    的管道 `$?`（sha256sum 对空输入的哈希，恒为 0）骗过读回校验。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    stamp = "v9-unlink"

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp,
        extra_env={"CASS_BACKUP_FAULT": "unlink-nas-db-before-readback"},
    )

    assert rc != 0, out
    assert (dest / f"INCOMPLETE-{stamp}").is_dir(), out
    assert not (dest / f".incomplete-{stamp}").exists(), out
    assert not list(dest.glob("cass-*")), out
    assert "readback" in out.lower(), out


def test_pipestatus_reflects_dd_failure_while_naive_dollar_question_lies():
    """反例断言（bash -c 演示，不依赖脚本/cass）：dd 读一个不存在的文件，
    `PIPESTATUS[0]` 非零，而管道整体 `$?`（sha256sum 对空输入的哈希，恒为 0）会
    撒谎说成功。且**先取一次 `$?` 再取 PIPESTATUS 会把数组本身冲成单元素**——必须
    `RC=("${PIPESTATUS[@]}")` 紧跟管道一次性整数组捕获（spec §6.4 / codex R2-P0）。
    """
    naive = subprocess.run(
        [
            "bash", "-c",
            "set +e +o pipefail\n"
            "dd if=/nonexistent-for-pipestatus-probe bs=1M iflag=direct status=none "
            "| sha256sum >/dev/null\n"
            "NAIVE=$?\n"
            'RC=("${PIPESTATUS[@]}")\n'
            'echo "naive=$NAIVE rc0=${RC[0]} rc1_is_set=${RC[1]+yes}"\n',
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert "naive=0" in naive.stdout, naive.stdout  # 反例：$? 撒谎说管道成功
    assert "rc0=0" in naive.stdout, naive.stdout  # 先取 $? 已经把数组冲成单元素
    assert "rc1_is_set=" not in naive.stdout or "rc1_is_set=yes" not in naive.stdout, naive.stdout

    correct = subprocess.run(
        [
            "bash", "-c",
            "set +e +o pipefail\n"
            "dd if=/nonexistent-for-pipestatus-probe bs=1M iflag=direct status=none "
            "| sha256sum >/dev/null\n"
            'RC=("${PIPESTATUS[@]}")\n'
            'echo "rc0=${RC[0]} rc1=${RC[1]}"\n',
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert "rc0=1 rc1=0" in correct.stdout, correct.stdout  # dd 失败(1)，sha256sum 成功(0)


# ---------------------------------------------------------------------------
# Step 2 — V11: blobs 池共享 + 幂等 + tmp/ 排除
# ---------------------------------------------------------------------------


@requires_cass
def test_v11_second_backup_transfers_zero_blob_files(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v11-first")
    assert rc1 == 0, out1
    assert "transferred" in out1, out1

    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v11-second")
    assert rc2 == 0, out2
    assert "transferred 0 files" in out2, out2


@requires_cass
def test_v11_raw_mirror_tmp_not_synced_to_nas(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    tmp_dir = synth_dd / "raw-mirror" / "v1" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_dir / "partial-upload.raw").write_bytes(b"should-never-reach-nas")

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v11-tmp")

    assert rc == 0, out
    assert not list(dest.rglob("partial-upload.raw")), (
        f"raw-mirror/v1/tmp/ 的内容绝不能出现在 NAS 上任何位置: {list(dest.rglob('*'))}"
    )


# ---------------------------------------------------------------------------
# Step 3 — manifests 门：V13 系列 + --publish-check 单元测试
# ---------------------------------------------------------------------------


@requires_cass
def test_v13_first_backup_manifest_snapshot_immutable_after_source_mutation(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """跑两次备份，其间源端合法改写某 manifest 的 db_links——第一份备份目录里的该
    manifest 内容必须原封不动（历史恢复点不可变），第二份是新内容。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    manifests_dir = synth_dd / "raw-mirror" / "v1" / "manifests"

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v13-first")
    assert rc1 == 0, out1

    target_name = sorted(manifests_dir.glob("*.json"))[0].name
    first_snapshot = (dest / ".incomplete-v13-first" / "manifests" / target_name).read_bytes()

    _mutate_manifest_db_links(manifests_dir / target_name)

    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v13-second")
    assert rc2 == 0, out2

    assert (dest / ".incomplete-v13-first" / "manifests" / target_name).read_bytes() == (
        first_snapshot
    ), "第一份备份目录里的 manifest 内容必须原封不动（历史恢复点不可变）"
    second_snapshot = (dest / ".incomplete-v13-second" / "manifests" / target_name).read_bytes()
    assert second_snapshot != first_snapshot, "第二份应反映源端的合法改写"


@requires_cass
def test_v13a_sha256sum_dash_c_passes_and_catches_same_size_mtime_tamper(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """`sha256sum -c manifests.sha256sum` 在 `.incomplete-*/` 内对健康快照全过；
    把某 manifest 篡改成同 size 同 mtime、不同内容 → 会被检出。对照演示：同一份
    篡改若走 `rsync -a`（quick-check 只看 size+mtime）会被当「没变」而跳过——
    这正是 manifests 用 `cp -a` 而非 `rsync -a` 的理由（R3-P2）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    stamp = "v13a"

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp)
    assert rc == 0, out

    incomplete = dest / f".incomplete-{stamp}"
    good = subprocess.run(
        ["sha256sum", "-c", "manifests.sha256sum"],
        cwd=incomplete, capture_output=True, text=True, timeout=30,
    )
    assert good.returncode == 0, good.stdout + good.stderr

    target = sorted((incomplete / "manifests").glob("*.json"))[0]
    original_bytes = target.read_bytes()
    original_stat = target.stat()
    tampered = bytearray(original_bytes)
    tampered[-2] ^= 0xFF  # 不改变长度，翻转倒数第二个字节
    target.write_bytes(bytes(tampered))
    os.utime(target, (original_stat.st_atime, original_stat.st_mtime))
    assert target.stat().st_size == len(original_bytes), "篡改必须保持同 size"

    bad = subprocess.run(
        ["sha256sum", "-c", "manifests.sha256sum"],
        cwd=incomplete, capture_output=True, text=True, timeout=30,
    )
    assert bad.returncode != 0, bad.stdout + bad.stderr
    assert "FAILED" in bad.stdout, bad.stdout

    # 对照：rsync -a 的 quick-check（只看 size+mtime）会把这份篡改当「没变」而跳过。
    demo_src = tmp_path / "rsync-demo-src"
    demo_dst = tmp_path / "rsync-demo-dst"
    demo_src.mkdir()
    demo_dst.mkdir()
    (demo_src / "m.json").write_bytes(bytes(tampered))
    (demo_dst / "m.json").write_bytes(original_bytes)
    same_time = (original_stat.st_atime, original_stat.st_mtime)
    os.utime(demo_src / "m.json", same_time)
    os.utime(demo_dst / "m.json", same_time)
    subprocess.run(
        ["rsync", "-a", f"{demo_src}/", f"{demo_dst}/"], check=True, timeout=30
    )
    assert (demo_dst / "m.json").read_bytes() == original_bytes, (
        "对照失败：rsync -a quick-check 应当把这份 size+mtime 相同的篡改当作「没变」而跳过"
    )


@requires_cass
def test_v13a2_swapped_self_consistent_manifest_passes_14b_but_fails_14a(
    synth_dd, tmp_path
):
    """V13a2——unit-level：把 `.incomplete/manifests/` 某文件整体换成另一个真实自洽
    manifest（引用一个真实存在、内容匹配的 blob）→ 14b 的四项检查全过（它校验的是
    替换后那份 manifest 自己的 blob），只有 `manifests.sha256sum`（14a）能抓出
    「内容被整体调包」。

    本用例走 unit-level（直接构造 `.incomplete` 形态的目录 + 直接调用
    `run_publish_check`/`verify_manifests_sha256sum`），不驱动完整脚本：这是一个
    TOCTOU 场景（内容必须在「manifests.sha256sum 已生成」之后、「14a 读它」之前被
    替换），本 task 的 `CASS_BACKUP_FAULT` 枚举写死只有 flip-nas-db /
    unlink-nas-db-before-readback 两点，不含针对 manifests 的钩子，无法从进程外
    构造出这一时序窗口——直接单测两个门各自的判据更精确也更稳定。
    """
    manifests_dir = synth_dd / "raw-mirror" / "v1" / "manifests"
    blobs_root = synth_dd / "raw-mirror" / "v1" / "blobs"

    incomplete = tmp_path / "fake-incomplete"
    incomplete.mkdir()
    shutil.copytree(manifests_dir, incomplete / "manifests")
    subprocess.run(
        "sha256sum manifests/*.json > manifests.sha256sum",
        shell=True, cwd=incomplete, check=True, timeout=30,
    )

    # 造一份真实自洽的替换 manifest：新 blob 内容 + 匹配的 blob_blake3/size/path。
    extra_content = b"v13a2-self-consistent-alternate-manifest-blob"
    extra_hash = blake3.blake3(extra_content).hexdigest()
    extra_blob_path = blobs_root / "blake3" / extra_hash[:2] / f"{extra_hash}.raw"
    extra_blob_path.parent.mkdir(parents=True, exist_ok=True)
    extra_blob_path.write_bytes(extra_content)

    replacement_manifest = {
        "schema_version": 1,
        "manifest_kind": "cass_raw_session_mirror_v1",
        "manifest_id": "v13a2-synthetic-replacement",
        "blob_hash_algorithm": "blake3",
        "blob_relative_path": f"blobs/blake3/{extra_hash[:2]}/{extra_hash}.raw",
        "blob_blake3": extra_hash,
        "blob_size_bytes": len(extra_content),
    }
    target = sorted((incomplete / "manifests").glob("*.json"))[0]
    target.write_text(json.dumps(replacement_manifest), encoding="utf-8")
    # manifests.sha256sum 里记的还是旧内容的哈希——不更新它，模拟「快照落地之后被整体调包」。

    ok_14b, problems_14b = cass_manifest_census.run_publish_check(
        incomplete / "manifests", blobs_root
    )
    assert ok_14b, f"14b 应四项全过（替换后的 manifest 自洽）: {problems_14b}"

    ok_14a, problems_14a = cass_manifest_census.verify_manifests_sha256sum(
        incomplete / "manifests", incomplete / "manifests.sha256sum"
    )
    assert not ok_14a, "14a 必须抓出 manifests.sha256sum 与被调包内容不符"
    assert any("checksum mismatch" in p for p in problems_14a), problems_14a


@requires_cass
def test_v13b_missing_blob_in_pool_fails_closure(synth_dd, tmp_path):
    """V13b——unit-level：manifest 引用的 blob 若不在 NAS 池里 → 闭合检查 FAIL。

    真实场景是「源端 blob 在 rsync 之前消失」的 TOCTOU（Tier 0 门在写锁内已验证过
    blob 存在，写锁释放后到 rsync 之间源端才被删）——本 task 的故障注入枚举不含
    此钩子（写死只有 flip-nas-db / unlink-nas-db-before-readback），直接单测
    `run_publish_check` 的存在性判据更精确。"""
    manifests_dir = synth_dd / "raw-mirror" / "v1" / "manifests"
    empty_blobs_root = tmp_path / "empty-blobs-pool"
    empty_blobs_root.mkdir()

    ok, problems = cass_manifest_census.run_publish_check(manifests_dir, empty_blobs_root)

    assert not ok
    assert any("blob 池文件缺失" in p for p in problems), problems


@requires_cass
def test_v13c_nas_pool_blob_truncated_to_zero_caught_by_st_size(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """先跑一次成功备份（把唯一 blob 落进共享池），直接在 NAS 池上把它截成 0 字节
    （模拟位腐/半截写——不经故障注入，纯外部文件操作），再跑第二次：
    `--ignore-existing` 会跳过重传（文件已存在），只有 14b 的 `st_size` 判据能挡住。
    """
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v13c-first")
    assert rc1 == 0, out1

    blob_files = list((dest / "raw-mirror" / "v1" / "blobs").glob("blake3/*/*.raw"))
    assert blob_files, out1
    blob_files[0].write_bytes(b"")

    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v13c-second")

    assert rc2 != 0, out2
    assert (dest / "INCOMPLETE-v13c-second").is_dir(), out2
    assert "st_size" in out2, out2


@requires_cass
def test_v13c2_nas_pool_blob_same_length_wrong_content_caught_by_blake3(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """同 V13c 的两轮备份手法，但这次篡改保持**同长度**——`st_size` 判据会通过，
    只有重算 BLAKE3 内容 hash 能抓出（内容寻址存储唯一判据）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v13c2-first")
    assert rc1 == 0, out1

    blob_files = list((dest / "raw-mirror" / "v1" / "blobs").glob("blake3/*/*.raw"))
    assert blob_files, out1
    original = blob_files[0].read_bytes()
    assert len(original) > 0
    corrupted = bytearray(original)
    corrupted[0] ^= 0xFF
    blob_files[0].write_bytes(bytes(corrupted))

    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v13c2-second")

    assert rc2 != 0, out2
    assert (dest / "INCOMPLETE-v13c2-second").is_dir(), out2
    assert "st_size 不符" not in out2, f"同长度篡改不应触发 st_size 判据: {out2}"
    assert "BLAKE3" in out2, out2


@requires_cass
def test_v13d_path_traversal_blob_relative_path_shape_fails_no_fs_side_effect(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """把某 manifest 的 `blob_relative_path` 改成 `../../etc/passwd`——14b 的形状
    校验必须 FAIL，且实现层面路径永远由 `blob_blake3` 推导（`blob_path_for`），
    该字段自始至终不参与任何文件系统操作。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    manifests_dir = synth_dd / "raw-mirror" / "v1" / "manifests"

    target = sorted(manifests_dir.glob("*.json"))[0]
    data = json.loads(target.read_text(encoding="utf-8"))
    data["blob_relative_path"] = "../../etc/passwd"
    target.write_text(json.dumps(data), encoding="utf-8")

    passwd_mtime_before = pathlib.Path("/etc/passwd").stat().st_mtime

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v13d")

    assert rc != 0, out
    assert (dest / "INCOMPLETE-v13d").is_dir(), out
    assert "形状不符" in out, out
    assert pathlib.Path("/etc/passwd").stat().st_mtime == passwd_mtime_before, (
        "blob_relative_path 绝不能参与任何文件系统操作"
    )


# ---------------------------------------------------------------------------
# --publish-check 子命令单元测试（不经全脚本）
# ---------------------------------------------------------------------------


def test_run_publish_check_passes_on_healthy_synth_dd(synth_dd):
    manifests_dir = synth_dd / "raw-mirror" / "v1" / "manifests"
    blobs_root = synth_dd / "raw-mirror" / "v1" / "blobs"

    ok, problems = cass_manifest_census.run_publish_check(manifests_dir, blobs_root)

    assert ok, problems
    assert problems == []


def test_run_publish_check_basename_mismatch_fails(synth_dd, tmp_path):
    """basename（去 .raw）与同一 manifest 的 blob_blake3 不一致 → FAIL（即便形状
    本身合法：另一个真实存在的 hash 目录结构）。"""
    manifests_dir = synth_dd / "raw-mirror" / "v1" / "manifests"
    blobs_root = synth_dd / "raw-mirror" / "v1" / "blobs"

    out_dir = tmp_path / "manifests-copy"
    shutil.copytree(manifests_dir, out_dir)
    target = sorted(out_dir.glob("*.json"))[0]
    data = json.loads(target.read_text(encoding="utf-8"))
    fake_hash = "a" * 64
    data["blob_relative_path"] = f"blobs/blake3/{fake_hash[:2]}/{fake_hash}.raw"
    target.write_text(json.dumps(data), encoding="utf-8")

    ok, problems = cass_manifest_census.run_publish_check(out_dir, blobs_root)

    assert not ok
    assert any("basename != blob_blake3" in p for p in problems), problems


def test_run_publish_check_unparseable_manifest_fails(synth_dd, tmp_path):
    manifests_dir = synth_dd / "raw-mirror" / "v1" / "manifests"
    blobs_root = synth_dd / "raw-mirror" / "v1" / "blobs"

    out_dir = tmp_path / "manifests-copy"
    shutil.copytree(manifests_dir, out_dir)
    target = sorted(out_dir.glob("*.json"))[0]
    target.write_text("{not valid json", encoding="utf-8")

    ok, problems = cass_manifest_census.run_publish_check(out_dir, blobs_root)

    assert not ok
    assert any("无法解析" in p for p in problems), problems


def test_publish_check_cli_pass(synth_dd):
    """真跑 CLI 子进程（e2e 覆盖参数解析 / exit code / stdout），`--doctor-json` 在
    `--publish-check` 模式下不需要提供。"""
    result = subprocess.run(
        [
            str(VENV_PY), str(CENSUS_SCRIPT), "--publish-check",
            "--manifests-dir", str(synth_dd / "raw-mirror" / "v1" / "manifests"),
            "--blobs-root", str(synth_dd / "raw-mirror" / "v1" / "blobs"),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "[PASS]" in result.stdout


def test_publish_check_cli_fail(synth_dd, tmp_path):
    empty_blobs_root = tmp_path / "empty-blobs"
    empty_blobs_root.mkdir()
    result = subprocess.run(
        [
            str(VENV_PY), str(CENSUS_SCRIPT), "--publish-check",
            "--manifests-dir", str(synth_dd / "raw-mirror" / "v1" / "manifests"),
            "--blobs-root", str(empty_blobs_root),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "[FAIL]" in result.stdout


def test_census_mode_still_requires_doctor_json(synth_dd):
    """非 `--publish-check` 模式（Tier 0 普查）仍然要求 `--doctor-json`——argparse
    usage 错误（exit 2），不是裸崩溃。"""
    result = subprocess.run(
        [
            str(VENV_PY), str(CENSUS_SCRIPT),
            "--manifests-dir", str(synth_dd / "raw-mirror" / "v1" / "manifests"),
            "--blobs-root", str(synth_dd / "raw-mirror" / "v1" / "blobs"),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "--doctor-json" in result.stderr
