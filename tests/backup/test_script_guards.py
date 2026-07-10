"""`infra/backup/backup-cass.sh` 骨架的端到端测试（Task 9：guards / 双锁 / 锁内三件 /
SUSPECT，spec §6 数据流 step 0-9）。

覆盖 Task 9 brief 的 Step 1-3：

  - V6：KEEP 非法整数、DEST 未挂载、DEST 不可写、staging tmpfs、staging 余量不足、
    自身锁并发 skip、TERM 跑中清理 staging。
  - V6a：外部持有 `.cass-write.lock` → `LOCK_WAIT` 超时 exit 非零，NAS 无 `cass-*/`。
  - V6b：stub doctor 睡眠期间，真 `index-pull.sh` 并发跑（HOME 派生锁）→ 秒退 skip。
  - V6c：持续写者 + `DB_TIMEOUT` 短 → `.backup` 失败（或超时），错误文案含 "timeout"。
  - 门失败路径两种语义分开测：五腿门失败落 `SUSPECT-*/`（db+census.tsv+gate.json，
    无 `COMPLETE`）；Tier 0 门失败零 NAS 产物（不落 SUSPECT/.incomplete）。
  - blake3 preflight：`CASS_BACKUP_VENV_PY` 指向裸 python3（无 blake3）→ 早期 exit 非零，
    断言 stub doctor 从未被调用（发生在 doctor 之前）。

大多数纯 guard 测试（KEEP/pairing/自身锁/tmpfs/staging 余量/DEST 挂载/可写探针/
blake3 preflight）用手搓的最小 `agent_search.db`（占位字节，只需可 `stat`），不依赖真
`cass` 二进制——这些 guard 在触及 raw-mirror/doctor 之前就已 exit。只有需要真实 schema
/ raw-mirror 语义的两个测试（五腿门 SUSPECT、Tier 0 门失败）用 `synth_dd`（真 cass 构建），
标 `requires_cass`。
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import shutil
import signal
import sqlite3
import subprocess
import threading
import time

import pytest

import cass_backup_gate
import cass_common
import cass_manifest_census
import fixture_factory

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "infra" / "backup" / "backup-cass.sh"

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd"
)


# ---------------------------------------------------------------------------
# 帮手：手搓最小 data_dir（占位 db，只需可 stat；不依赖真 cass），供纯 guard 测试用。
# ---------------------------------------------------------------------------


def _fake_data_dir(tmp_path: pathlib.Path, size_bytes: int = 4096) -> pathlib.Path:
    data_dir = tmp_path / "fake-data-dir"
    data_dir.mkdir()
    (data_dir / "agent_search.db").write_bytes(b"\0" * size_bytes)
    return data_dir


def _popen_backup(home: pathlib.Path, env: dict[str, str]) -> subprocess.Popen:
    merged = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
    merged.update(env)
    return subprocess.Popen(
        ["bash", str(SCRIPT)],
        env=merged,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _write_verified_doctor_stub(home: pathlib.Path, manifests_dir: pathlib.Path) -> None:
    """按 manifests_dir 的真实普查数造一份「与 census 恒等式吻合」的 doctor stub JSON
    （五腿门/Tier 0 测试专用——Tier 0 门必须先 PASS 才能走到五腿门）。"""
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


def _write_sleepy_cass_stub(bin_dir: pathlib.Path, sleep_s: float, doc: dict) -> None:
    """自定义 `cass` stub（不复用 conftest 的 `cass_stub`——那份不支持 sleep）：
    `cass doctor ...` 先睡 `sleep_s` 秒再吐出 `doc` 的 JSON，退出 0。"""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "cass"
    payload = json.dumps(doc)
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [ "${1:-}" = "doctor" ]; then\n'
        f"  sleep {sleep_s}\n"
        f"  cat <<'BACKUP_CASS_STUB_EOF'\n{payload}\nBACKUP_CASS_STUB_EOF\n"
        "  exit 0\n"
        "fi\n"
        'echo "unsupported stub invocation: $*" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _publish_baseline(dest: pathlib.Path, name: str, gate_json: dict, census_path, generation: int) -> None:
    """把一次 gate CLI 的产出组装成 `<dest>/<name>/` 下「已发布备份」（含 COMPLETE +
    digest.json），供下一次 gate 调用当基线（同 test_gate_cli.py 的 `_publish_baseline`
    手法）。"""
    backup_dir = dest / name
    backup_dir.mkdir(parents=True)
    shutil.copy(census_path, backup_dir / "census.tsv")
    gate_json = dict(gate_json)
    gate_json["generation"] = generation
    (backup_dir / "digest.json").write_bytes(cass_common.dumps_canonical(gate_json))
    (backup_dir / "COMPLETE").touch()


# ---------------------------------------------------------------------------
# Step 1 — V6: 参数校验 / 人工通道成对性 / staging guard / NAS guard / 自身锁 / TERM
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("keep_value", ["0", "abc"])
def test_keep_invalid_exits_nonzero(tmp_home, run_backup, tmp_path, keep_value):
    rc, out, _dest = run_backup(
        env={
            "CASS_BACKUP_KEEP": keep_value,
            "CASS_BACKUP_STAGING": str(tmp_path / "staging"),
        }
    )
    assert rc != 0, out


@pytest.mark.parametrize(
    "var",
    [
        "CASS_BACKUP_REBASELINE",
        "CASS_BACKUP_ADOPT_SESSIONS",
        "CASS_BACKUP_QUARANTINE_SESSIONS",
        "CASS_BACKUP_RETENTION_RESET",
    ],
)
def test_manual_channel_missing_reason_exits_nonzero(tmp_home, run_backup, tmp_path, var):
    """四组人工通道任一只给值不给 reason（或反过来）→ 拒绝运行（spec §5.7 成对约束）。"""
    rc, out, _dest = run_backup(
        env={var: "some-value", "CASS_BACKUP_STAGING": str(tmp_path / "staging")}
    )
    assert rc != 0, out


def test_dest_not_mounted_exits_nonzero(tmp_home, run_backup, tmp_path):
    """DEST 落在 $HOME/nas/ 前缀下，但 tmp HOME 下这只是个普通目录，不是真挂载点
    （backup-gbrain.sh 同构 guard；spec §6 step 3）。不覆盖 CASS_BACKUP_DEST，用默认值
    命中 conftest 预建的 `home/nas/openclaw/backups/cass`。"""
    data_dir = _fake_data_dir(tmp_path)
    rc, out, _dest = run_backup(
        env={
            "CASS_DATA_DIR": str(data_dir),
            "CASS_BACKUP_STAGING": str(tmp_path / "staging"),
        }
    )
    assert rc != 0, out


def test_dest_not_writable_exits_nonzero(tmp_home, run_backup, tmp_path):
    data_dir = _fake_data_dir(tmp_path)
    dest = tmp_path / "readonly-dest"
    dest.mkdir()
    dest.chmod(0o500)
    try:
        rc, out, _dest = run_backup(
            env={
                "CASS_DATA_DIR": str(data_dir),
                "CASS_BACKUP_DEST": str(dest),
                "CASS_BACKUP_STAGING": str(tmp_path / "staging"),
            }
        )
        assert rc != 0, out
    finally:
        dest.chmod(0o700)


def test_staging_tmpfs_guard_exits_nonzero(tmp_home, run_backup):
    """`/dev/shm` 是真 tmpfs——命中前不需要 CASS_DATA_DIR（guard 顺序：fstype 先于
    db 存在性检查，见 backup-cass.sh step 2）。"""
    lock_path = pathlib.Path("/dev/shm/.backup-cass.self.lock")
    try:
        rc, out, _dest = run_backup(env={"CASS_BACKUP_STAGING": "/dev/shm"})
        assert rc != 0, out
    finally:
        lock_path.unlink(missing_ok=True)


def test_staging_insufficient_margin_exits_nonzero(tmp_home, run_backup, tmp_path):
    """把 db 换成稀疏大文件（apparent size，不占真实磁盘块）让 3x 超过 df 余量。"""
    staging = tmp_path / "staging"
    staging.mkdir()
    avail = shutil.disk_usage(staging).free
    data_dir = _fake_data_dir(tmp_path, size_bytes=0)
    subprocess.run(
        ["truncate", "-s", str(avail), str(data_dir / "agent_search.db")], check=True
    )

    rc, out, _dest = run_backup(
        env={
            "CASS_DATA_DIR": str(data_dir),
            "CASS_BACKUP_DEST": str(tmp_path / "dest"),
            "CASS_BACKUP_STAGING": str(staging),
        }
    )
    assert rc != 0, out


def test_self_lock_concurrent_second_instance_skips(tmp_home, run_backup, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    lock_fd = open(staging / ".backup-cass.self.lock", "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        rc, out, _dest = run_backup(env={"CASS_BACKUP_STAGING": str(staging)})
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
    assert rc == 0, out
    assert "skip" in out.lower(), out


def test_term_signal_mid_run_cleans_staging(tmp_home, tmp_path):
    """`kill -TERM` 跑中（doctor 睡眠窗口内）→ trap 显式 exit 143 + EXIT trap 清理
    staging，无半成品目录残留。"""
    stub_dir = tmp_path / "sleepy-cass-bin"
    doc = {
        "raw_mirror": {
            "status": "verified",
            "summary": {
                "missing_blob_count": 0,
                "checksum_mismatch_count": 0,
                "manifest_checksum_mismatch_count": 0,
                "invalid_manifest_count": 0,
                "interrupted_capture_count": 0,
                "manifest_count": 0,
                "verified_blob_count": 0,
                "duplicate_blob_reference_count": 0,
            },
        }
    }
    _write_sleepy_cass_stub(stub_dir, sleep_s=5, doc=doc)

    data_dir = _fake_data_dir(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    stamp = "termtest"

    proc = _popen_backup(
        tmp_home,
        {
            "CASS_DATA_DIR": str(data_dir),
            "CASS_BACKUP_DEST": str(dest),
            "CASS_BACKUP_STAGING": str(staging),
            "CASS_BACKUP_STAMP": stamp,
            "CASS_BACKUP_LOCK_WAIT": "30",
            "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    try:
        deadline = time.monotonic() + 8
        stg_dirs: list[pathlib.Path] = []
        while time.monotonic() < deadline:
            stg_dirs = list(staging.glob(f"cass-backup-{stamp}.*"))
            if stg_dirs:
                break
            time.sleep(0.1)
        assert stg_dirs, "STG 未在预期时间内出现，测试前提不成立（doctor 睡眠窗口应已建好 staging）"

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert rc != 0, proc.stdout.read() if proc.stdout else ""
    assert not list(staging.glob(f"cass-backup-{stamp}.*")), "TERM 后 staging 必须已清理干净，无半成品"


# ---------------------------------------------------------------------------
# Step 2 — V6a/V6b/V6c
# ---------------------------------------------------------------------------


def test_write_lock_busy_exits_nonzero_v6a(tmp_home, run_backup, tmp_path):
    data_dir = _fake_data_dir(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    lock_dir = tmp_home / ".local" / "share"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_dir / ".cass-write.lock", "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        rc, out, _dest = run_backup(
            env={
                "CASS_DATA_DIR": str(data_dir),
                "CASS_BACKUP_DEST": str(dest),
                "CASS_BACKUP_STAGING": str(tmp_path / "staging"),
                "CASS_BACKUP_LOCK_WAIT": "2",
            }
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    assert rc != 0, out
    assert not list(dest.glob("cass-*")), "写锁busy 不应产生任何 NAS 备份产物"


def test_concurrent_index_pull_skips_while_doctor_holds_lock_v6b(tmp_home, tmp_path):
    """stub doctor 睡眠 5s 期间，真 `infra/cass-semantic/index-pull.sh` 并发跑
    （同一 HOME 派生同一把 `.cass-write.lock`）→ 秒退，stdout 含
    `"skipped":"another cass write holds lock"`。"""
    stub_dir = tmp_path / "sleepy-cass-bin"
    doc = {
        "raw_mirror": {
            "status": "verified",
            "summary": {
                "missing_blob_count": 0,
                "checksum_mismatch_count": 0,
                "manifest_checksum_mismatch_count": 0,
                "invalid_manifest_count": 0,
                "interrupted_capture_count": 0,
                "manifest_count": 0,
                "verified_blob_count": 0,
                "duplicate_blob_reference_count": 0,
            },
        }
    }
    _write_sleepy_cass_stub(stub_dir, sleep_s=5, doc=doc)

    data_dir = _fake_data_dir(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()

    backup_proc = _popen_backup(
        tmp_home,
        {
            "CASS_DATA_DIR": str(data_dir),
            "CASS_BACKUP_DEST": str(dest),
            "CASS_BACKUP_STAGING": str(staging),
            "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    try:
        # 等写锁真正被 backup-cass.sh 持有（doctor 睡眠窗口内）：轮询锁文件出现且
        # index-pull.sh 自己尝试 flock -n 会失败（即锁已被占）。给 1s 起步余量。
        lock_path = tmp_home / ".local" / "share" / ".cass-write.lock"
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not lock_path.exists():
            time.sleep(0.05)
        time.sleep(0.3)  # 确保 backup-cass.sh 已经 flock 成功、正处在 doctor sleep 里

        index_pull = REPO / "infra" / "cass-semantic" / "index-pull.sh"
        result = subprocess.run(
            ["bash", str(index_pull)],
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_home)},
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        backup_proc.wait(timeout=30)

    assert result.returncode == 0, result.stdout + result.stderr
    assert '"skipped":"another cass write holds lock"' in result.stdout, result.stdout


def _build_big_wal_db(db_path: pathlib.Path, target_mb: int = 100) -> None:
    """造一个够大的 WAL 模式 db，让 `.backup` 有可测量的耗时窗口——cass 的
    `agent_search.db` 实测是 WAL 模式（非默认 rollback journal）；WAL 模式下 backup
    reader 对并发写者的 MVCC 隔离很强，微型库的 `.backup` 常常在一次内部 step 里就偷跑
    完，写者根本来不及插进去（实测 5/5 trial 全部瞬间 rc=0）。库大到几十/百 MB 级、
    `.backup` 天然要花几百 ms 以上，紧循环写者才有机会在 step 之间插入提交，逼真重现
    spec §2.2「写者不停则永不收敛」（实测 100MB + 紧循环写者 5/5 trial 稳定 timeout）。"""
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t(x)")
        blob = "a" * 100_000  # ~100KB/行
        n_rows = target_mb * 10  # 10 行 ≈ 1MB
        con.executemany("INSERT INTO t VALUES (?)", ((blob,) for _ in range(n_rows)))
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


def _hammer_writer(db_path: pathlib.Path, stop_event: threading.Event) -> None:
    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        while not stop_event.is_set():
            con.execute("INSERT INTO t VALUES (?)", ("b" * 1000,))
            con.commit()
    finally:
        con.close()


def test_db_timeout_with_persistent_writer_v6c(tmp_home, run_backup, tmp_path):
    """spec §2.2：`.backup` 在源库持续被写时不收敛（sqlite3_backup_step 被并发提交
    重启）——够大的库 + 紧循环写者，`timeout $DB_TIMEOUT` 兜住，FATAL 文案含
    "timeout" 字样。不依赖真 cass 二进制（手搓 WAL db，只测 `.backup` 本身的锁/超时
    行为，脚本在此步之后才会碰 raw-mirror/五腿门）。"""
    data_dir = tmp_path / "big-data-dir"
    data_dir.mkdir()
    db_path = data_dir / "agent_search.db"
    _build_big_wal_db(db_path, target_mb=100)

    stub_dir = tmp_path / "fast-cass-bin"
    _write_sleepy_cass_stub(stub_dir, sleep_s=0, doc={})

    stop_event = threading.Event()
    writer = threading.Thread(target=_hammer_writer, args=(db_path, stop_event), daemon=True)
    writer.start()
    time.sleep(0.1)  # 给写者一点头启动时间，确保它已经在紧循环里

    try:
        dest = tmp_path / "dest"
        rc, out, _dest = run_backup(
            env={
                "CASS_DATA_DIR": str(data_dir),
                "CASS_BACKUP_DEST": str(dest),
                "CASS_BACKUP_STAGING": str(tmp_path / "staging"),
                "CASS_BACKUP_DB_TIMEOUT": "3",
                "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
        )
    finally:
        stop_event.set()
        writer.join(timeout=10)

    assert rc != 0, out
    assert "timeout" in out.lower(), out


# ---------------------------------------------------------------------------
# Step 3 — 门失败路径：五腿门 → SUSPECT 取证；Tier 0 门 → 零产物；blake3 preflight 早死
# ---------------------------------------------------------------------------


@requires_cass
def test_five_leg_gate_failure_lands_suspect_with_forensics(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """攻击②（清空 messages.content）相对既有基线 → leg4 前缀摘要不符 → 五腿门 FAIL
    → `$DEST/SUSPECT-<stamp>/` 落 db+census.tsv+gate.json，无 COMPLETE（spec §5.7/§6
    step 9 取证路径；digest.json 版取证是 Task 13 的升级点，本 task 到 gate.json 为止）。"""
    dest = tmp_path / "dest"
    dest.mkdir()

    db = synth_dd / "agent_search.db"
    census1 = tmp_path / "census1.tsv"
    gate1 = tmp_path / "gate1.json"
    rc0 = cass_backup_gate.main(
        [
            "--db", str(db),
            "--dest", str(dest),
            "--out-census", str(census1),
            "--out-gate-json", str(gate1),
        ]
    )
    assert rc0 == 0, "基线本身不应 FAIL"
    _publish_baseline(dest, "cass-baseline", json.loads(gate1.read_bytes()), census1, generation=1)

    fixture_factory.attack2(db)
    _write_verified_doctor_stub(tmp_home, synth_dd / "raw-mirror" / "v1" / "manifests")

    rc, out, _dest = run_backup(
        env={
            "CASS_DATA_DIR": str(synth_dd),
            "CASS_BACKUP_DEST": str(dest),
            "CASS_BACKUP_STAGING": str(tmp_path / "staging"),
            "PATH": f"{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
        }
    )

    assert rc != 0, out
    suspects = list(dest.glob("SUSPECT-*"))
    assert len(suspects) == 1, out
    susp = suspects[0]
    assert (susp / "db").is_file()
    assert (susp / "census.tsv").is_file()
    assert (susp / "gate.json").is_file()
    assert not (susp / "COMPLETE").exists()
    assert "[leg 4] FAIL" in out, out


@requires_cass
def test_tier0_gate_failure_zero_nas_artifacts(tmp_home, run_backup, synth_dd, cass_stub, tmp_path):
    """stub doctor 喂 `status:"warn"` → Tier 0 门 FAIL（与五腿门失败语义不同）→ exit 非零
    且 DEST 零产物（无 SUSPECT-*、无 .incomplete-*——Tier 0 门失败发生在锁内、发生在
    任何 NAS 写入之前）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    doc = {
        "raw_mirror": {
            "status": "warn",
            "summary": {
                "missing_blob_count": 0,
                "checksum_mismatch_count": 0,
                "manifest_checksum_mismatch_count": 0,
                "invalid_manifest_count": 0,
                "interrupted_capture_count": 0,
                "manifest_count": 0,
                "verified_blob_count": 0,
                "duplicate_blob_reference_count": 0,
            },
        }
    }
    (tmp_home / ".cass-stub-doctor.json").write_text(json.dumps(doc), encoding="utf-8")

    rc, out, _dest = run_backup(
        env={
            "CASS_DATA_DIR": str(synth_dd),
            "CASS_BACKUP_DEST": str(dest),
            "CASS_BACKUP_STAGING": str(tmp_path / "staging"),
            "PATH": f"{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
        }
    )

    assert rc != 0, out
    assert "Tier 0 gate failed" in out, out
    assert list(dest.iterdir()) == [], f"Tier 0 门失败必须零 NAS 产物: {list(dest.iterdir())}"


def test_blake3_preflight_fails_before_doctor_invoked(tmp_home, run_backup, tmp_path):
    """`CASS_BACKUP_VENV_PY` 覆盖成裸 `/usr/bin/python3`（无 blake3）→ 早期 exit 非零，
    发生在 doctor 被调用之前（stub `cass` 一旦被调用就会 touch 一个标记文件；测试断言
    该标记从未出现）。"""
    stub_dir = tmp_path / "fake-cass-bin"
    stub_dir.mkdir()
    stub = stub_dir / "cass"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'touch "$HOME/.cass-was-invoked"\n'
        "exit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    rc, out, _dest = run_backup(
        env={
            "CASS_BACKUP_STAGING": str(tmp_path / "staging"),
            "CASS_BACKUP_VENV_PY": "/usr/bin/python3",
            "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        }
    )

    assert rc != 0, out
    assert not (tmp_home / ".cass-was-invoked").exists(), (
        "blake3 preflight 必须在 doctor 被调用之前拦截"
    )


# ---------------------------------------------------------------------------
# Review 修复回归：gate exit 2（用法/环境错）≠ exit 1（数据 FAIL）——绝不落 SUSPECT
# ---------------------------------------------------------------------------


@requires_cass
def test_gate_exit2_env_error_no_suspect_orphan(tmp_home, run_backup, synth_dd, cass_stub, tmp_path):
    """rebaseline 目标非法 → gate exit 2（此时 census/gate.json 根本没写）。修复前
    脚本把 2 并进 DB_GATE_FAIL：SUSPECT 块 `cp` 不存在的文件被 set -e 中途击杀 →
    NAS 留半拉 `SUSPECT-*/db` 孤儿 + FATAL 消息没打出来。修后：exit 非零、输出指认
    usage/env 错 + gate 自己的 rebaseline stderr 可见、DEST 零 SUSPECT（无孤儿）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    _write_verified_doctor_stub(tmp_home, synth_dd / "raw-mirror" / "v1" / "manifests")

    rc, out, _dest = run_backup(
        env={
            "CASS_DATA_DIR": str(synth_dd),
            "CASS_BACKUP_DEST": str(dest),
            "CASS_BACKUP_STAGING": str(tmp_path / "staging"),
            "CASS_BACKUP_REBASELINE": "cass-genuinely-does-not-exist",
            "CASS_BACKUP_REBASELINE_REASON": "exit-2 regression test",
            "PATH": f"{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
        }
    )

    assert rc != 0, out
    assert "gate usage/env error (rc=2)" in out, out
    assert "rebaseline" in out, f"gate 自己的 stderr（环境错根因）必须可见，不能被埋: {out}"
    assert not list(dest.glob("SUSPECT-*")), (
        f"exit 2 环境错绝不落 SUSPECT（含半拉孤儿）: {list(dest.iterdir())}"
    )


# ---------------------------------------------------------------------------
# Review 覆盖缺口：step 4 陈旧 .incomplete-* 的两条分支（DEV-6 RECOVERABLE / 直接 rm）
# ---------------------------------------------------------------------------


def _make_stale_incomplete(dest: pathlib.Path, name: str, with_complete: bool) -> pathlib.Path:
    """预造 mtime 2 天前的 `.incomplete-<name>/`，内含载荷文件（可选顶层 COMPLETE）。"""
    stale = dest / f".incomplete-{name}"
    stale.mkdir(parents=True)
    (stale / "db").write_bytes(b"precious payload bytes")
    if with_complete:
        (stale / "COMPLETE").touch()
    two_days_ago = time.time() - 2 * 86400
    os.utime(stale, (two_days_ago, two_days_ago))
    return stale


@requires_cass
def test_stale_incomplete_with_complete_becomes_recoverable_and_alerts(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """DEV-6：含顶层 COMPLETE 的陈旧 `.incomplete-*` → `mv -T` 成 `RECOVERABLE-<同名尾巴>`
    （内容原封不动），当晚备份照常继续走到临时成功出口，但最终 exit 非零（ALERT_FLAG，
    告警不丢）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    stale = _make_stale_incomplete(dest, "old", with_complete=True)
    _write_verified_doctor_stub(tmp_home, synth_dd / "raw-mirror" / "v1" / "manifests")

    rc, out, _dest = run_backup(
        env={
            "CASS_DATA_DIR": str(synth_dd),
            "CASS_BACKUP_DEST": str(dest),
            "CASS_BACKUP_STAGING": str(tmp_path / "staging"),
            # Task 12 起首晚需要显式 ADOPT（spec §6.3.1 step 13a）——本测试关注
            # DEV-6 的 RECOVERABLE 救援 + ALERT_FLAG，与 sessions 通道正交。
            "CASS_BACKUP_ADOPT_SESSIONS": "1",
            "CASS_BACKUP_ADOPT_REASON": "test fixture — sessions channel not under test here",
            "PATH": f"{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
        }
    )

    assert rc != 0, out  # 备份本身成功也要 exit 非零——告警不丢（DEV-6）
    recoverable = dest / "RECOVERABLE-old"
    assert recoverable.is_dir(), out
    assert (recoverable / "COMPLETE").exists()
    assert (recoverable / "db").read_bytes() == b"precious payload bytes", "载荷必须原封不动"
    assert not stale.exists(), "原 .incomplete-* 应已被改名走"
    assert "gate passed" in out, f"当晚备份应照常继续到临时成功出口: {out}"
    assert "RECOVERABLE" in out, out


@requires_cass
def test_stale_incomplete_without_complete_removed_and_backup_succeeds(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """不含 COMPLETE 的陈旧 `.incomplete-*` = 半成品垃圾 → rm -rf，当晚备份正常 exit 0。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    stale = _make_stale_incomplete(dest, "old2", with_complete=False)
    _write_verified_doctor_stub(tmp_home, synth_dd / "raw-mirror" / "v1" / "manifests")

    rc, out, _dest = run_backup(
        env={
            "CASS_DATA_DIR": str(synth_dd),
            "CASS_BACKUP_DEST": str(dest),
            "CASS_BACKUP_STAGING": str(tmp_path / "staging"),
            # Task 12 起首晚需要显式 ADOPT（spec §6.3.1 step 13a）——本测试关注陈旧
            # `.incomplete-*` 清理，与 sessions 通道正交。
            "CASS_BACKUP_ADOPT_SESSIONS": "1",
            "CASS_BACKUP_ADOPT_REASON": "test fixture — sessions channel not under test here",
            "PATH": f"{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
        }
    )

    assert rc == 0, out
    assert not stale.exists(), "无 COMPLETE 的陈旧半成品应被清掉"
    assert not list(dest.glob("RECOVERABLE-*")), "无 COMPLETE 不应走 RECOVERABLE 救援"
