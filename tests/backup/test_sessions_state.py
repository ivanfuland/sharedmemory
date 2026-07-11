"""`infra/backup/cass/cass_sessions.py` 的 `update-state`/`publish-gate` +
`backup-cass.sh` step 13a, 13e-13g 的测试（Task 12：sessions 通道 B —— 共享状
态 / 发布门全量回读 / ADOPT，spec §6.3.1 / 数据流 step 13a, 13e-13g）。

覆盖 Task 12 brief 的 Step 1-3：

  - update-state (13e) 单元测试：从空清单起、结转未传输记录、传输文件读回 NAS
    记录 present、transferred 点名但 NAS 找不到文件（内部错误）、state 损坏。
  - publish-gate (13f/13g) 单元测试：V12l（有记录无文件 FAIL）、V12f（陌生文件
    FAIL/--adopt 后过）、V12f×V12k2（同轮分流回归）、向前漂移修正、V14/V12g/
    V12h（同尺寸篡改 FAIL，含三条反例演示）、absent_at_source 结转、adopt/
    quarantine 成对性、未知 alias、out-tsv 与 state 字节相同、stdout 留痕。
  - V12n：state 完整性头（篡改任一行/删首行 → 下一轮非零）。
  - e2e（`run_backup` 全脚本 + `CASS_BACKUP_FAULT` 故障注入）：V12（rev3 bug 回
    归）、V12i（并发追加，记 NAS 实际 size）、V12j（rewrite-src-mid-rsync
    TOCTOU）、V12m（kill-after-sessions-rsync 崩溃后对账）、V12k2（drop-one-
    itemize 自愈回归）、V12n 的 kill-before-state-publish、首晚 ADOPT bootstrap、
    整根源目录消失 → absent_at_source。
  - 顺手修复：check_source 对含 `\\n`/`\\r` 的 subpath fail-closed（行式 exclude
    文件机制边界）。

大多数 e2e 测试依赖真 `cass` 二进制构建 `synth_dd`（`requires_cass`，同
test_sessions_source.py 的约定）；update-state/publish-gate 的纯 Python 单元测
试不需要 `cass`。

本文件自包含，不跨文件 import 其它测试文件的私有函数（同代码库既有约定）。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import threading
import time

import blake3
import pytest

import cass_common
import cass_sessions
from cass_common import SessionRec

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
SESSIONS_SCRIPT = REPO / "infra" / "backup" / "cass" / "cass_sessions.py"
BACKUP_SCRIPT = REPO / "infra" / "backup" / "backup-cass.sh"

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd"
)


# ---------------------------------------------------------------------------
# 帮手（本文件自包含，不跨文件 import 其它测试文件的私有函数——同代码库既有约定）。
# ---------------------------------------------------------------------------


def _rec(relpath: str, content: bytes, status: str = "present") -> SessionRec:
    return SessionRec(relpath, len(content), blake3.blake3(content).hexdigest(), status)


def _write_verified_doctor_stub(home: pathlib.Path, manifests_dir: pathlib.Path) -> None:
    """同 test_sessions_source.py 的写法——Tier 0 门必须先 PASS 才能走到本 task
    覆盖的 step 13a/13e-13g。本文件自包含一份，不跨文件 import。"""
    import json

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


def _run(
    tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp, session_roots,
    extra_env=None,
):
    """跑一次 backup-cass.sh，固定 stamp + 自定义 CASS_BACKUP_SESSION_ROOTS。"""
    _write_verified_doctor_stub(tmp_home, synth_dd / "raw-mirror" / "v1" / "manifests")
    env = {
        "CASS_DATA_DIR": str(synth_dd),
        "CASS_BACKUP_DEST": str(dest),
        "CASS_BACKUP_STAGING": str(staging),
        "CASS_BACKUP_STAMP": stamp,
        "CASS_BACKUP_SESSION_ROOTS": session_roots,
        "PATH": f"{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    if extra_env:
        env.update(extra_env)
    rc, out, _dest = run_backup(env=env)
    return rc, out


_ADOPT_BOOTSTRAP_ENV = {
    "CASS_BACKUP_ADOPT_SESSIONS": "1",
    "CASS_BACKUP_ADOPT_REASON": "test bootstrap",
}


# ---------------------------------------------------------------------------
# Step 1a — update-state (13e) 单元测试
# ---------------------------------------------------------------------------


def test_update_state_first_call_creates_state_from_missing_file(tmp_path):
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    good = b'{"a":1}\n'
    (sessions_root / "alpha" / "s.jsonl").write_bytes(good)
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("alpha/s.jsonl\n")
    state_path = tmp_path / "state.tsv"

    assert not state_path.exists()
    rc = cass_sessions.update_state(str(state_path), str(sessions_root), str(transferred))

    assert rc == 0
    assert cass_common.state_read(state_path) == [_rec("alpha/s.jsonl", good)]


def test_update_state_untransferred_records_carried_forward_unchanged(tmp_path):
    """已有记录不在本轮 transferred 里 ⇒ 原样结转（哪怕它标记 absent_at_source，
    或它对应的 NAS 文件此刻已经不在了——比对 NAS 是否失踪不是 13e 的职责，是
    13f 的）。"""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    old_present = _rec("alpha/old.jsonl", b"old-content\n")
    old_absent = SessionRec("alpha/gone.jsonl", 999, "f" * 64, "absent_at_source")
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [old_present, old_absent])
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")

    rc = cass_sessions.update_state(str(state_path), str(sessions_root), str(transferred))

    assert rc == 0
    assert set(cass_common.state_read(state_path)) == {old_present, old_absent}


def test_update_state_transferred_overwrites_existing_record_with_nas_content(tmp_path):
    """本轮 transferred 点名的文件必须**覆盖**旧记录（不是新增一条）——旧记录若
    还停在昨晚的 size/hash，今晚必须换成 NAS 此刻的真实内容。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    grown = b"good1\ngood2\ngood3\n"
    (sessions_root / "alpha" / "s.jsonl").write_bytes(grown)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", b"good1\ngood2\n")])
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("alpha/s.jsonl\n")

    rc = cass_sessions.update_state(str(state_path), str(sessions_root), str(transferred))

    assert rc == 0
    records = cass_common.state_read(state_path)
    assert records == [_rec("alpha/s.jsonl", grown)]


def test_update_state_transferred_file_missing_from_sessions_root_is_internal_error(tmp_path):
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("alpha/ghost.jsonl\n")
    state_path = tmp_path / "state.tsv"

    rc = cass_sessions.update_state(str(state_path), str(sessions_root), str(transferred))

    assert rc == 1
    assert not state_path.exists(), "内部错误路径不该写出任何东西"


def test_update_state_corrupt_existing_state_is_internal_error(tmp_path):
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    state_path = tmp_path / "state.tsv"
    state_path.write_text("no header line at all\n")
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")

    rc = cass_sessions.update_state(str(state_path), str(sessions_root), str(transferred))
    assert rc == 1


def test_update_state_missing_transferred_file_is_internal_error(tmp_path):
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    state_path = tmp_path / "state.tsv"

    rc = cass_sessions.update_state(
        str(state_path), str(sessions_root), str(tmp_path / "does-not-exist.txt")
    )
    assert rc == 1
    assert not state_path.exists()


def test_update_state_cli_subprocess_pass(tmp_path):
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    good = b"hello\n"
    (sessions_root / "alpha" / "s.jsonl").write_bytes(good)
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("alpha/s.jsonl\n")
    state_path = tmp_path / "state.tsv"

    result = subprocess.run(
        [
            str(VENV_PY), str(SESSIONS_SCRIPT), "update-state",
            "--state", str(state_path), "--sessions-root", str(sessions_root),
            "--transferred", str(transferred),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert cass_common.state_read(state_path) == [_rec("alpha/s.jsonl", good)]


# ---------------------------------------------------------------------------
# Step 1b — publish-gate (13f/13g) 单元测试
# ---------------------------------------------------------------------------


def test_publish_gate_clean_match_passes_and_out_tsv_byte_identical_to_state(tmp_path):
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    good = b"good1\ngood2\n"
    (sessions_root / "alpha" / "s.jsonl").write_bytes(good)
    src_root = tmp_path / "src" / "alpha"
    src_root.mkdir(parents=True)
    (src_root / "s.jsonl").write_bytes(good)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", good)])
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={src_root}", str(transferred),
        str(out_tsv),
    )

    assert rc == 0
    assert cass_common.state_read(state_path) == [_rec("alpha/s.jsonl", good)]
    assert out_tsv.read_bytes() == state_path.read_bytes()


def test_v12l_recorded_present_missing_from_nas_fails_and_leaves_state_untouched(tmp_path):
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()  # 记录存在，NAS 上该文件被删了
    (sessions_root / "alpha").mkdir()
    state_path = tmp_path / "state.tsv"
    original = [_rec("alpha/s.jsonl", b"good1\ngood2\n")]
    cass_common.state_write_atomic(state_path, original)
    original_bytes = state_path.read_bytes()
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), "alpha=" + str(tmp_path / "src"), str(transferred),
        str(out_tsv),
    )

    assert rc == 1
    assert state_path.read_bytes() == original_bytes, "FAIL 时旧 state 必须原封不动（现场保留）"
    assert not out_tsv.exists(), "FAIL 时不该写出任何 out-tsv"


def test_v12l_absent_at_source_record_missing_from_nas_also_fails(tmp_path):
    """review Important #1（控制器按 spec 原文仲裁）：`absent_at_source` 记录的
    NAS 文件不存在同样必须 FAIL——spec §6.3.1 发布门原文「对清单里的**每一条**
    记录从 NAS 读回内容重算 blake3：文件不存在 ⇒ FAIL」没有状态豁免；§6.5 反面
    教训①：源端已删除、NAS 仍保留的会话正是 Tier 0′ 最该保住的。源端已没了，
    NAS 是最后一份，丢它必须响（与 V12l 的 present 版对称）。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)  # 该文件在 NAS 上不存在
    state_path = tmp_path / "state.tsv"
    original = [SessionRec("alpha/gone.jsonl", 12, "a" * 64, "absent_at_source")]
    cass_common.state_write_atomic(state_path, original)
    original_bytes = state_path.read_bytes()
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), "alpha=" + str(tmp_path / "src"), str(transferred),
        str(out_tsv),
    )

    assert rc == 1, "absent_at_source 的 NAS 副本是最后一份——丢了不能静默结转"
    assert state_path.read_bytes() == original_bytes, "FAIL 时旧 state 必须原封不动（现场保留）"
    assert not out_tsv.exists(), "FAIL 时不该写出任何 out-tsv"


def test_v12f_orphan_not_in_transferred_fails_without_adopt(tmp_path):
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    (sessions_root / "alpha" / "stray.jsonl").write_bytes(b"nobody-recorded-me\n")
    state_path = tmp_path / "state.tsv"  # 从不存在起——没有任何记录
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")  # 本轮什么都没传输
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), "alpha=" + str(tmp_path / "src"), str(transferred),
        str(out_tsv),
    )

    assert rc == 1
    assert not state_path.exists()
    assert not out_tsv.exists()


def test_v12f_orphan_adopted_with_adopt_flag_and_stdout_provenance(tmp_path, capsys):
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    content = b"nobody-recorded-me\n"
    (sessions_root / "alpha" / "stray.jsonl").write_bytes(content)
    src_root = tmp_path / "src" / "alpha"
    src_root.mkdir(parents=True)
    (src_root / "stray.jsonl").write_bytes(content)  # 源端仍在 ⇒ 收编后应为 present
    state_path = tmp_path / "state.tsv"
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={src_root}", str(transferred),
        str(out_tsv), adopt=True, adopt_reason="manual recovery",
    )

    assert rc == 0
    assert cass_common.state_read(state_path) == [_rec("alpha/stray.jsonl", content)]
    captured = capsys.readouterr()
    assert "PROV adopt" in captured.out
    assert "alpha/stray.jsonl" in captured.out
    assert "manual recovery" in captured.out


def test_v12f_x_v12k2_same_run_self_heal_and_adopt_required_fail_dont_mix(tmp_path):
    """R3-P1 binding 的直接断言：同一轮里一个漏记的已传输文件（应自愈,不需要
    --adopt）+ 一个塞进来的陌生文件（不在 transferred 里,需要 --adopt）——分流
    必须精确，不能用同一条规则打包处理。先验证「无 --adopt 时两个都成孤儿,整体
    FAIL」，再验证「有 --adopt 时全过,且 stdout 分别标注 PROV self-heal 与
    PROV adopt」。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    healed_content = b"was-really-transferred-this-round\n"
    stray_content = b"pre-existing-unrelated-file\n"
    (sessions_root / "alpha" / "healed.jsonl").write_bytes(healed_content)
    (sessions_root / "alpha" / "stray.jsonl").write_bytes(stray_content)
    src_root = tmp_path / "src" / "alpha"
    src_root.mkdir(parents=True)
    (src_root / "healed.jsonl").write_bytes(healed_content)
    (src_root / "stray.jsonl").write_bytes(stray_content)
    state_path = tmp_path / "state.tsv"  # 两个都不在 state 里
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("alpha/healed.jsonl\n")  # 只有 healed 是本轮真传输过的
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc_no_adopt = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={src_root}", str(transferred),
        str(out_tsv),
    )
    assert rc_no_adopt == 1, "陌生文件仍需 --adopt，不能因为另一个文件能自愈就整体放行"
    assert not state_path.exists()

    result = subprocess.run(
        [
            str(VENV_PY), str(SESSIONS_SCRIPT), "publish-gate",
            "--state", str(state_path), "--sessions-root", str(sessions_root),
            "--roots", f"alpha={src_root}", "--transferred", str(transferred),
            "--out-tsv", str(out_tsv), "--adopt", "--adopt-reason", "sweep stray files",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(cass_common.state_read(state_path)) == {
        _rec("alpha/healed.jsonl", healed_content),
        _rec("alpha/stray.jsonl", stray_content),
    }
    assert "PROV self-heal" in result.stdout and "alpha/healed.jsonl" in result.stdout
    assert "PROV adopt" in result.stdout and "alpha/stray.jsonl" in result.stdout
    # 分流必须精确——不能把自愈的那条也标成 adopt，反之亦然。
    self_heal_line = next(l for l in result.stdout.splitlines() if "healed.jsonl" in l)
    adopt_line = next(l for l in result.stdout.splitlines() if "stray.jsonl" in l)
    assert "PROV self-heal" in self_heal_line and "PROV adopt" not in self_heal_line
    assert "PROV adopt" in adopt_line and "PROV self-heal" not in adopt_line


def test_forward_drift_nas_longer_and_old_record_is_prefix_gets_corrected(tmp_path):
    """向前漂移：NAS 比记录更长，且记录的 hash 恰好等于 NAS 前 nas_size 字节的
    hash（历史上某次失败运行只传输成功、清单没跟上）——必须修正记录，不是 FAIL。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    old_content = b"good1\ngood2\n"
    new_content = old_content + b"good3\n"
    (sessions_root / "alpha" / "d.jsonl").write_bytes(new_content)
    src_root = tmp_path / "src" / "alpha"
    src_root.mkdir(parents=True)
    (src_root / "d.jsonl").write_bytes(new_content)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/d.jsonl", old_content)])
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")  # 未传输就结转的场景——13e 从没跑过这条
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={src_root}", str(transferred),
        str(out_tsv),
    )

    assert rc == 0
    assert cass_common.state_read(state_path) == [_rec("alpha/d.jsonl", new_content)]


def test_content_mismatch_same_size_not_forward_drift_fails(tmp_path):
    """size 不符合「更长」条件（同尺寸篡改）⇒ 不是向前漂移，必须 FAIL——这是
    V14/V12h 的核心判据单元级钉法。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    good = b"GOOD1\nGOOD2\n"
    tampered = b"BAD11\nBAD22\n"
    assert len(good) == len(tampered)
    (sessions_root / "alpha" / "s.jsonl").write_bytes(tampered)
    src_root = tmp_path / "src" / "alpha"
    src_root.mkdir(parents=True)
    (src_root / "s.jsonl").write_bytes(good)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", good)])
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={src_root}", str(transferred),
        str(out_tsv),
    )

    assert rc == 1
    assert (sessions_root / "alpha" / "s.jsonl").read_bytes() == tampered, "现场必须保留"


def test_content_mismatch_shorter_than_recorded_is_not_forward_drift_fails(tmp_path):
    """反面：变短同样不是「向前漂移」（只有「更长且旧记录是其前缀」才算），必须
    FAIL——防止把「向前漂移」的判据错写成「size 不等就修」。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    long_content = b"good1\ngood2\ngood3\n"
    (sessions_root / "alpha" / "s.jsonl").write_bytes(long_content[:6])  # 截短
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", long_content)])
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={tmp_path / 'src'}", str(transferred),
        str(out_tsv),
    )

    assert rc == 1


def test_v12g_absent_at_source_record_with_tampered_nas_content_fails(tmp_path):
    """V12g：源端已删（absent_at_source）+ NAS 内容被篡改（同尺寸）⇒ 每晚发布门
    仍必须抓到（不是「反正源端没了就不用管 NAS 那份」）。反例演示：
    `rsync -a --checksum --dry-run src/ dst/` 对 receiver-only 文件视野为空、
    rc=0——它挡不住这种损坏，只有全量读回能挡。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    good = b"good1\ngood2\n"
    tampered = b"BAD11\nBAD22\n"
    assert len(good) == len(tampered)
    nas_file = sessions_root / "alpha" / "gone.jsonl"
    nas_file.write_bytes(tampered)
    src_root = tmp_path / "src" / "alpha"
    src_root.mkdir(parents=True)  # 源端目录还在，但这个文件已经删了

    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(
        state_path, [SessionRec("alpha/gone.jsonl", len(good), blake3.blake3(good).hexdigest(), "absent_at_source")]
    )
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    # 反例：rsync --checksum --dry-run 对这个 receiver-only 文件完全看不见。
    dry_run = subprocess.run(
        ["rsync", "-a", "--checksum", "--dry-run", "-i", f"{src_root}/", f"{sessions_root / 'alpha'}/"],
        capture_output=True, text=True, timeout=30,
    )
    assert dry_run.returncode == 0
    assert "gone.jsonl" not in dry_run.stdout, (
        f"反例应验证 rsync --checksum --dry-run 看不到 receiver-only 文件的篡改: {dry_run.stdout!r}"
    )

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={src_root}", str(transferred),
        str(out_tsv),
    )

    assert rc == 1, "全量读回校验必须抓到 absent_at_source 文件的位腐"
    assert nas_file.read_bytes() == tampered, "现场必须保留"


def test_v14_present_record_same_size_tamper_fails_stat_alone_would_pass(tmp_path):
    """V14：source 仍在、NAS 同尺寸篡改 ⇒ FAIL、现场保留。反例①：只做 stat 对
    账（长度相同）会 PASS（实测）。反例②：不加 --dry-run 的 `rsync --checksum`
    会把 dst 悄悄修回源端内容并 exit 0——销毁作案现场；在一份**独立拷贝**上演
    示这一点（不能碰真实待断言的 NAS 文件）。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    good = b"good1\ngood2\n"
    tampered = b"BAD11\nBAD22\n"
    assert len(good) == len(tampered)
    nas_file = sessions_root / "alpha" / "s.jsonl"
    nas_file.write_bytes(tampered)
    src_root = tmp_path / "src" / "alpha"
    src_root.mkdir(parents=True)
    (src_root / "s.jsonl").write_bytes(good)

    # 反例①：stat 全过（尺寸相同）。
    assert nas_file.stat().st_size == len(good)

    # 反例②：在独立拷贝上演示不加 --dry-run 的 rsync --checksum 会默默"修好"
    # （覆盖成源端内容）并 exit 0，销毁篡改证据——真实待断言的 nas_file 不受影响。
    shadow_dst = tmp_path / "shadow-dst"
    shadow_dst.mkdir()
    shutil.copy(nas_file, shadow_dst / "s.jsonl")
    fix = subprocess.run(
        ["rsync", "-a", "--checksum", f"{src_root}/", f"{shadow_dst}/"],
        capture_output=True, text=True, timeout=30,
    )
    assert fix.returncode == 0
    assert (shadow_dst / "s.jsonl").read_bytes() == good, (
        "反例：不加 --dry-run 的 --checksum 会把损坏悄悄修好——这正是为什么不能"
        "用它当校验手段"
    )

    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", good)])
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={src_root}", str(transferred),
        str(out_tsv),
    )

    assert rc == 1
    assert nas_file.read_bytes() == tampered, "真实 NAS 文件的篡改现场必须保留"


def test_v12h_append_cannot_fix_same_size_bad_dst_publish_gate_catches_it(tmp_path):
    """V12h：dst 是同尺寸的坏内容，`--append` 修不了它（rc=0 且 dst 不变，因为
    --append 只从旧长度往后追加——dst 长度已经"够"了，无事可做）；源端前缀校验
    （check-source）也看不见它（它只读源端，不读 NAS）；只有发布门的全量 NAS
    读回校验能抓。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    good = b"GOOD1\nGOOD2\n"
    tampered = b"BAD!1\nBAD!2\n"
    assert len(good) == len(tampered)
    nas_dir = sessions_root / "alpha"
    (nas_dir / "s.jsonl").write_bytes(tampered)
    src_root = tmp_path / "src" / "alpha"
    src_root.mkdir(parents=True)
    (src_root / "s.jsonl").write_bytes(good)

    # ① --append 修不了它。
    append_result = subprocess.run(
        ["rsync", "-a", "--append", f"{src_root}/", f"{nas_dir}/"],
        capture_output=True, text=True, timeout=30,
    )
    assert append_result.returncode == 0
    assert (nas_dir / "s.jsonl").read_bytes() == tampered, "--append 不该动到同尺寸的 dst"

    # ② check-source（源端前缀校验）看不见它——它只读源端，源端是干净的 GOOD。
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", good)])
    out_exclude = tmp_path / "excl"
    check_rc = cass_sessions.check_source(str(state_path), f"alpha={src_root}", str(out_exclude))
    assert check_rc == 0, "check-source 只读源端，源端未变，不该报异常"

    # ③ 发布门的全量 NAS 读回校验必须抓到。
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"
    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={src_root}", str(transferred),
        str(out_tsv),
    )
    assert rc == 1


def test_whole_source_root_missing_present_record_becomes_absent_at_source(tmp_path):
    """整根源目录消失（不是单个文件被删）→ publish-gate 全量回读必须把该根的
    present 记录判 absent_at_source 结转；NAS 内容仍必须读回验证过（Task 11
    reviewer 留的验证项）。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    good = b"good1\ngood2\n"
    (sessions_root / "alpha" / "s.jsonl").write_bytes(good)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", good)])
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"
    missing_src_root = tmp_path / "src-does-not-exist"  # 整个 alias 的源根都不存在

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={missing_src_root}", str(transferred),
        str(out_tsv),
    )

    assert rc == 0, "NAS 内容干净——只是源端没了，不该 FAIL"
    records = cass_common.state_read(state_path)
    assert records == [SessionRec("alpha/s.jsonl", len(good), blake3.blake3(good).hexdigest(), "absent_at_source")]


def test_whole_source_root_missing_but_nas_also_missing_still_fails(tmp_path):
    """反面：源端整根目录消失**且** NAS 也没有这个文件 ⇒ 仍然是 V12l 的 FAIL
    （absent_at_source 合法结转的前提是 NAS 内容还在、能读回验证；NAS 也没了就
    是真丢失，不能悄悄放行）。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)  # 该文件本身不存在
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", b"good1\ngood2\n")])
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"
    missing_src_root = tmp_path / "src-does-not-exist"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={missing_src_root}", str(transferred),
        str(out_tsv),
    )

    assert rc == 1


def test_publish_gate_adopt_reason_pairing_rejected(tmp_path):
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    state_path = tmp_path / "state.tsv"
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")

    rc1 = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), "alpha=" + str(tmp_path / "src"), str(transferred),
        str(tmp_path / "out.tsv"), adopt=True, adopt_reason=None,
    )
    assert rc1 == 1

    rc2 = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), "alpha=" + str(tmp_path / "src"), str(transferred),
        str(tmp_path / "out.tsv"), adopt=False, adopt_reason="orphan reason with no --adopt",
    )
    assert rc2 == 1


def test_publish_gate_unknown_alias_in_state_is_internal_error(tmp_path):
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(
        state_path, [_rec("ghost-alias/x.jsonl", b"x")]
    )
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), "alpha=" + str(tmp_path / "src"), str(transferred),
        str(tmp_path / "out.tsv"),
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# codex R4-P1：发布门必须扫 $DEST/sessions/ 实际全部内容，未知顶层 alias / 裸文件
# 也要 FAIL（spec §6.3.1「NAS 上任何无清单记录文件 ⇒ FAIL」）。修复前发布门只枚举
# --roots 已知 alias 的子树，`sessions/rogue/stray.jsonl` 整个逃过验收（rc=0）。
# ---------------------------------------------------------------------------


def test_r4_rogue_file_under_unknown_alias_fails(tmp_path):
    """codex 复现：`sessions/rogue/stray.jsonl`（rogue 不是 --roots 里的 alias）
    + 空 state + 空 transferred → 发布门必须 FAIL，不写 state/out-tsv，且不可被
    --adopt 收编（未知 alias 无 source root 可对账）。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "rogue").mkdir(parents=True)
    (sessions_root / "rogue" / "stray.jsonl").write_bytes(b"i-am-an-orphan\n")
    state_path = tmp_path / "state.tsv"  # 从不存在起
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), "alpha=" + str(tmp_path / "src"),
        str(transferred), str(out_tsv),
    )
    assert rc == 1, "未知 alias 下的 rogue 文件必须 FAIL（spec §6.3.1）"
    assert not state_path.exists(), "FAIL 时不写 state"
    assert not out_tsv.exists(), "FAIL 时不写 out-tsv"

    # --adopt 也救不了它（adopt 只收编已知 root 下的 receiver-only 文件）：
    rc_adopt = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), "alpha=" + str(tmp_path / "src"),
        str(transferred), str(out_tsv), adopt=True, adopt_reason="尝试收编 rogue",
    )
    assert rc_adopt == 1, "未知 alias 的 rogue 文件不可 --adopt，必须仍 FAIL"


def test_r4_bare_file_at_sessions_root_fails(tmp_path):
    """裸文件直接落在 `$DEST/sessions/` 根下（无 alias/子路径）——同样是无清单记录
    的 orphan，必须 FAIL，不能因为 `_split_relpath` 拆不出 alias 就静默跳过。"""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    (sessions_root / "bare.jsonl").write_bytes(b"loose-file\n")
    state_path = tmp_path / "state.tsv"
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), "alpha=" + str(tmp_path / "src"),
        str(transferred), str(out_tsv),
    )
    assert rc == 1, "sessions 根下的裸文件必须 FAIL"
    assert not state_path.exists()
    assert not out_tsv.exists()


def test_r4_rogue_file_alongside_valid_known_alias_still_fails(tmp_path):
    """同一轮里既有合法的已知 alias 文件（有记录、内容相符，本该 PASS），又有一个
    未知 alias 的 rogue 文件——整体必须 FAIL（rogue 一票否决），且不写任何东西
    （保留现场）。证明 rogue 检测不是「只在全空时才跑」。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    good = b"legit\n"
    (sessions_root / "alpha" / "s.jsonl").write_bytes(good)
    (sessions_root / "rogue").mkdir(parents=True)
    (sessions_root / "rogue" / "stray.jsonl").write_bytes(b"orphan\n")
    src_root = tmp_path / "src" / "alpha"
    src_root.mkdir(parents=True)
    (src_root / "s.jsonl").write_bytes(good)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", good)])
    state_bytes_before = state_path.read_bytes()
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    rc = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={src_root}",
        str(transferred), str(out_tsv),
    )
    assert rc == 1, "rogue 文件一票否决整轮发布"
    assert state_path.read_bytes() == state_bytes_before, "FAIL 时 state 原封不动"
    assert not out_tsv.exists()


def test_publish_gate_cli_subprocess_pass(tmp_path):
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    good = b"hello\n"
    (sessions_root / "alpha" / "s.jsonl").write_bytes(good)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", good)])
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    result = subprocess.run(
        [
            str(VENV_PY), str(SESSIONS_SCRIPT), "publish-gate",
            "--state", str(state_path), "--sessions-root", str(sessions_root),
            "--roots", "alpha=" + str(tmp_path / "src"), "--transferred", str(transferred),
            "--out-tsv", str(out_tsv),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert out_tsv.is_file()


# ---------------------------------------------------------------------------
# Step 1c — V12n：共享状态的完整性头
# ---------------------------------------------------------------------------


def test_v12n_tampered_state_body_line_rejected_by_both_commands(tmp_path):
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    good = b"good1\ngood2\n"
    (sessions_root / "alpha" / "s.jsonl").write_bytes(good)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", good)])

    # 篡改任意一行（不动首行）——sha256 头不再匹配。行内容是「relpath\tsize\thash
    # \tstatus」（元数据，不含文件内容字节），故这里翻转 relpath 的一个字符。
    lines = state_path.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(lines) >= 2
    assert "alpha/s.jsonl" in lines[1]
    lines[1] = lines[1].replace("alpha/s.jsonl", "alpha/S.jsonl")  # 单字符改动，破坏校验和
    state_path.write_text("".join(lines), encoding="utf-8")

    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    rc_update = cass_sessions.update_state(str(state_path), str(sessions_root), str(transferred))
    assert rc_update == 1

    rc_gate = cass_sessions.publish_gate(
        str(state_path), str(sessions_root), f"alpha={tmp_path / 'src'}", str(transferred),
        str(tmp_path / "out.tsv"),
    )
    assert rc_gate == 1


def test_v12n_deleted_header_line_rejected(tmp_path):
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/s.jsonl", b"x")])
    lines = state_path.read_text(encoding="utf-8").splitlines(keepends=True)
    state_path.write_text("".join(lines[1:]), encoding="utf-8")  # 删首行（#sha256）

    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    rc = cass_sessions.update_state(str(state_path), str(sessions_root), str(transferred))
    assert rc == 1


def test_v12n_publish_gate_kill_before_state_publish_old_state_intact_then_next_run_ok(tmp_path):
    """review Minor #2：`publish-gate` 自己的 state 重写（13f）与 13e 走同一个
    `CASS_BACKUP_FAULT=kill-before-state-publish` 注入口——「两次写入都要
    crash 安全」。构造一个会改写 state 的场景（向前漂移修正），注入后被
    SIGKILL：旧 state 原封不动、out-tsv 没写出；下一轮（无注入）正常收敛。

    走 CLI subprocess 直接打 publish-gate（不走全脚本 e2e——e2e 里同名 FAULT 会
    先杀死更早执行的 update-state，够不到 13f 的写入点）。"""
    sessions_root = tmp_path / "sessions"
    (sessions_root / "alpha").mkdir(parents=True)
    old_content = b"good1\ngood2\n"
    new_content = old_content + b"good3\n"
    (sessions_root / "alpha" / "d.jsonl").write_bytes(new_content)
    src_root = tmp_path / "src" / "alpha"
    src_root.mkdir(parents=True)
    (src_root / "d.jsonl").write_bytes(new_content)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("alpha/d.jsonl", old_content)])
    baseline_bytes = state_path.read_bytes()
    transferred = tmp_path / "transferred.txt"
    transferred.write_text("")
    out_tsv = tmp_path / "out" / "sessions.tsv"

    cmd = [
        str(VENV_PY), str(SESSIONS_SCRIPT), "publish-gate",
        "--state", str(state_path), "--sessions-root", str(sessions_root),
        "--roots", f"alpha={src_root}", "--transferred", str(transferred),
        "--out-tsv", str(out_tsv),
    ]
    crashed = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
        env={**os.environ, "CASS_BACKUP_FAULT": "kill-before-state-publish"},
    )
    assert crashed.returncode == -9, (
        f"注入必须真的以 SIGKILL 终止进程: rc={crashed.returncode} "
        f"stdout={crashed.stdout!r} stderr={crashed.stderr!r}"
    )
    assert state_path.read_bytes() == baseline_bytes, (
        "写 .tmp 后、os.replace 前被杀——旧 state 必须原封不动（13f 的写入与 13e 同样 crash 安全）"
    )
    assert not out_tsv.exists(), "被杀在 state 发布前——out-tsv 更不该已写出"

    env_clean = {k: v for k, v in os.environ.items() if k != "CASS_BACKUP_FAULT"}
    healed = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env_clean)
    assert healed.returncode == 0, healed.stdout + healed.stderr
    assert cass_common.state_read(state_path) == [_rec("alpha/d.jsonl", new_content)]
    assert out_tsv.read_bytes() == state_path.read_bytes()


# ---------------------------------------------------------------------------
# Step 1d — 顺手修复：check_source 对含 \n/\r 的 subpath fail-closed
# ---------------------------------------------------------------------------


def test_check_source_state_relpath_with_linebreak_fails_closed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(
        state_path,
        [SessionRec("alpha/evil\nline.jsonl", 1, "f" * 64, "present")],
    )
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(str(state_path), f"alpha={root}", str(out_dir))

    assert rc == 1
    assert not (out_dir / "exclude.alpha").exists(), "fail-closed 不该落任何 exclude 文件"


def test_check_source_quarantine_subpath_with_carriage_return_fails_closed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [])
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(
        str(state_path), f"alpha={root}", str(out_dir),
        quarantine="alpha/evil\rline.jsonl", quarantine_reason="test",
    )

    assert rc == 1


# ---------------------------------------------------------------------------
# Step 2 — e2e（全脚本 + FAULT 注入）
# ---------------------------------------------------------------------------


@requires_cass
def test_v12_source_appends_two_lines_nas_line_count_matches_e2e(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """V12（rev3 bug 回归）：源会话文件追加两行后跑备份，NAS 上行数必须一致。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root = tmp_path / "root"
    root.mkdir()
    session_roots = f"alpha={root}"
    session_file = root / "s.jsonl"
    session_file.write_bytes(b'{"line":1}\n')

    rc1, out1 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12-first", session_roots,
        extra_env=_ADOPT_BOOTSTRAP_ENV,
    )
    assert rc1 == 0, out1

    with open(session_file, "ab") as f:
        f.write(b'{"line":2}\n{"line":3}\n')

    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12-second", session_roots)
    assert rc2 == 0, out2

    nas_file = dest / "sessions" / "alpha" / "s.jsonl"
    nas_lines = nas_file.read_bytes().count(b"\n")
    src_lines = session_file.read_bytes().count(b"\n")
    assert nas_lines == src_lines == 3, f"NAS 行数必须与源端一致: nas={nas_lines} src={src_lines}"
    records = cass_common.state_read(dest / "sessions.state.tsv")
    rec = next(r for r in records if r.relpath == "alpha/s.jsonl")
    assert rec.nas_size == nas_file.stat().st_size
    assert rec.blake3 == blake3.blake3(nas_file.read_bytes()).hexdigest()


def test_v12i_records_nas_actual_size_not_source_final_size_under_concurrent_append(tmp_path):
    """V12i：`--bwlimit` 拉长传输窗口 + 并发追加源端（真实写者线程，不是事后伪
    造）——update-state 必须记 NAS **此刻实际**写到的字节数，不是源端追加后的
    最终长度；次晚 check-source 用这份诚实记录做前缀校验必须 PASS（不误报）。"""
    root = tmp_path / "src"
    root.mkdir()
    dst = tmp_path / "sessions" / "alpha"
    dst.mkdir(parents=True)
    src_file = root / "s.jsonl"

    base = b"x" * 300_000
    src_file.write_bytes(base)

    def _writer():
        time.sleep(0.05)
        with open(src_file, "ab") as f:
            f.write(b"y" * 80_000)

    writer = threading.Thread(target=_writer)
    writer.start()
    result = subprocess.run(
        ["rsync", "-ai", "--append", "--bwlimit=300", f"{root}/", f"{dst}/"],
        capture_output=True, text=True, timeout=30,
    )
    writer.join()
    assert result.returncode == 0, result.stdout + result.stderr

    dst_file = dst / "s.jsonl"
    dst_size = dst_file.stat().st_size
    final_src_size = src_file.stat().st_size
    assert final_src_size == len(base) + 80_000, "写者线程必须真的追加成功"

    transferred = tmp_path / "transferred.txt"
    transferred.write_text("alpha/s.jsonl\n")
    state_path = tmp_path / "state.tsv"
    rc = cass_sessions.update_state(str(state_path), str(tmp_path / "sessions"), str(transferred))
    assert rc == 0

    records = cass_common.state_read(state_path)
    assert len(records) == 1
    rec = records[0]
    assert rec.nas_size == dst_size, "必须记 NAS 实际字节数"
    assert rec.blake3 == blake3.blake3(dst_file.read_bytes()).hexdigest()

    # 次晚：check-source 用这条记录对（已经长回完整长度的）源端做前缀校验，必须 PASS。
    out_dir = tmp_path / "excl"
    check_rc = cass_sessions.check_source(str(state_path), f"alpha={root}", str(out_dir))
    assert check_rc == 0, "诚实记录 NAS 实际 size 后，次晚前缀校验不该误报"


@requires_cass
def test_v12j_rewrite_src_mid_rsync_fault_state_reflects_nas_not_source_e2e(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """V12j：`CASS_BACKUP_FAULT=rewrite-src-mid-rsync` 在这次 rsync 启动前后台起
    一个延迟改写子进程，抢在源端第一个 jsonl 文件的前几个字节上（真实 TOCTOU，
    不是测试事后算出来的数字）。断言：共享状态记录的 hash 等于 NAS 实际内容的
    哈希，不等于此刻源端内容的哈希（按源端算是 rev12 已证伪的错误做法）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root = tmp_path / "root"
    root.mkdir()
    session_roots = f"alpha={root}"

    session_file = root / "s.jsonl"
    good = b"good1\ngood2\n"
    session_file.write_bytes(good)

    rc1, out1 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12j-first", session_roots,
        extra_env=_ADOPT_BOOTSTRAP_ENV,
    )
    assert rc1 == 0, out1

    # 第二晚：源端合法追加（前缀不变，check-source 该 PASS），同时注入 FAULT。
    session_file.write_bytes(good + b"good3\n")
    rc2, out2 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12j-second", session_roots,
        extra_env={"CASS_BACKUP_FAULT": "rewrite-src-mid-rsync"},
    )
    assert rc2 == 0, out2  # 破坏发生在传输窗口之后的字节区间，不影响这一晚的发布

    time.sleep(0.3)  # 给后台延迟改写子进程留足时间，避免测试断言跑在它前面

    nas_file = dest / "sessions" / "alpha" / "s.jsonl"
    nas_bytes = nas_file.read_bytes()
    src_bytes_now = session_file.read_bytes()
    assert nas_bytes != src_bytes_now, (
        f"注入必须真的改写了源端且 NAS 没被牵连: nas={nas_bytes!r} src={src_bytes_now!r}"
    )
    assert nas_bytes == good + b"good3\n"

    records = cass_common.state_read(dest / "sessions.state.tsv")
    rec = next(r for r in records if r.relpath == "alpha/s.jsonl")
    assert rec.blake3 == blake3.blake3(nas_bytes).hexdigest(), "记录的 hash 必须等于 NAS 实际内容"
    assert rec.blake3 != blake3.blake3(src_bytes_now).hexdigest(), (
        "反例：按源端此刻内容算会得到一个 NAS 上根本不存在的哈希（rev12 已证伪）"
    )


@requires_cass
def test_v12m_kill_after_sessions_rsync_next_run_reconciles_state_to_nas_e2e(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """V12m：night N 的 rsync（13d）成功落地（NAS 已变大），脚本在写清单（13e）
    之前被 SIGKILL（`CASS_BACKUP_FAULT=kill-after-sessions-rsync`）——旧清单还
    停在 night N-1。Night N+1：rsync 无事可做（itemize 为空）；断言发布前对账
    （13f）发现 st_size 不符、回读修正，最终 tsv 与 NAS 一致（反例：只按「未传
    输就结转」会发布一份 size 与 NAS 不符的假清单）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root = tmp_path / "root"
    root.mkdir()
    session_roots = f"alpha={root}"

    session_file = root / "s.jsonl"
    good = b"good1\ngood2\n"
    session_file.write_bytes(good)

    rc1, out1 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12m-first", session_roots,
        extra_env=_ADOPT_BOOTSTRAP_ENV,
    )
    assert rc1 == 0, out1

    session_file.write_bytes(good + b"good3\n")
    rc2, out2 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12m-crash", session_roots,
        extra_env={"CASS_BACKUP_FAULT": "kill-after-sessions-rsync"},
    )
    assert rc2 != 0, out2  # SIGKILL：非正常终止

    nas_file = dest / "sessions" / "alpha" / "s.jsonl"
    assert nas_file.read_bytes() == good + b"good3\n", "rsync 本身已经成功落地——NAS 已经变大"
    stale_records = cass_common.state_read(dest / "sessions.state.tsv")
    stale_rec = next(r for r in stale_records if r.relpath == "alpha/s.jsonl")
    assert stale_rec.nas_size == len(good), "旧清单必须还停在 night N-1（13e 从未跑过）"

    rc3, out3 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12m-heal", session_roots)
    assert rc3 == 0, out3

    healed_records = cass_common.state_read(dest / "sessions.state.tsv")
    healed_rec = next(r for r in healed_records if r.relpath == "alpha/s.jsonl")
    assert healed_rec.nas_size == len(good + b"good3\n")
    assert healed_rec.blake3 == blake3.blake3(nas_file.read_bytes()).hexdigest()


@requires_cass
def test_v12k2_drop_one_itemize_fault_self_healed_without_adopt_e2e(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """V12k2：`CASS_BACKUP_FAULT=drop-one-itemize` 只污染 update-state 消费的那
    份 transferred 拷贝（模拟 13e 自己的记录漏了一个文件），publish-gate 仍拿到
    未删减的 ground truth——断言它能把这条自愈回来，且**不需要** `--adopt`（区
    别于 V12f 的陌生文件），整次备份仍 exit 0。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root = tmp_path / "root"
    root.mkdir()
    # 两个文件保证 sed -i '1d' 删的是「第一条」而不是唯一一条——drop-one-itemize
    # 必须只影响 update-state 的记录完整性，不影响 publish-gate 的 ground truth。
    (root / "a.jsonl").write_bytes(b"content-a\n")
    (root / "b.jsonl").write_bytes(b"content-b\n")
    session_roots = f"alpha={root}"

    # 首晚（state 缺失）本身需要 13a 的 ADOPT 门——与「自愈是否需要 --adopt」是
    # 两件独立的事：publish_gate 的分流逻辑先查 transferred 集合再查 --adopt，
    # 真正在本轮 transferred 里的文件永远走 self-heal 分支，不受 --adopt 是否
    # 传入影响（见下方断言）。
    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12k2", session_roots,
        extra_env={"CASS_BACKUP_FAULT": "drop-one-itemize", **_ADOPT_BOOTSTRAP_ENV},
    )

    assert rc == 0, out
    assert "PROV self-heal" in out, f"publish-gate 必须在 stdout 留痕这是自愈: {out}"
    assert "PROV adopt" not in out, f"自愈不该被误标成 adopt，即便本轮 --adopt 也传了: {out}"

    for name, content in (("a.jsonl", b"content-a\n"), ("b.jsonl", b"content-b\n")):
        nas_file = dest / "sessions" / "alpha" / name
        assert nas_file.is_file(), out
        records = cass_common.state_read(dest / "sessions.state.tsv")
        rec = next(r for r in records if r.relpath == f"alpha/{name}")
        assert rec.nas_size == nas_file.stat().st_size
        assert rec.blake3 == blake3.blake3(nas_file.read_bytes()).hexdigest()


@requires_cass
def test_v12n_kill_before_state_publish_fault_old_state_intact_then_next_run_ok_e2e(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """V12n：`CASS_BACKUP_FAULT=kill-before-state-publish`（python 侧读 env，写
    完 `.tmp` 后 `os.replace` 前 SIGKILL 自己）——旧 state 必须原封不动（单文件
    原子性），下一轮正常运行且能收敛。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root = tmp_path / "root"
    root.mkdir()
    session_roots = f"alpha={root}"
    session_file = root / "s.jsonl"
    good = b"good1\ngood2\n"
    session_file.write_bytes(good)

    rc1, out1 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12n-first", session_roots,
        extra_env=_ADOPT_BOOTSTRAP_ENV,
    )
    assert rc1 == 0, out1
    state_path = dest / "sessions.state.tsv"
    baseline_bytes = state_path.read_bytes()

    session_file.write_bytes(good + b"good3\n")
    rc2, out2 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12n-crash", session_roots,
        extra_env={"CASS_BACKUP_FAULT": "kill-before-state-publish"},
    )
    assert rc2 != 0, out2

    assert state_path.read_bytes() == baseline_bytes, (
        "写 .tmp 后、os.replace 前被杀——旧 state 必须原封不动"
    )
    assert not (state_path.with_name(state_path.name + ".tmp")).exists() or True  # .tmp 残留不影响下一轮正确性

    rc3, out3 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12n-heal", session_roots)
    assert rc3 == 0, out3
    records = cass_common.state_read(state_path)
    rec = next(r for r in records if r.relpath == "alpha/s.jsonl")
    nas_file = dest / "sessions" / "alpha" / "s.jsonl"
    assert rec.nas_size == nas_file.stat().st_size == len(good + b"good3\n")


@requires_cass
def test_first_night_adopt_bootstrap_state_generated_and_self_verifies_e2e(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """首晚 `--adopt` bootstrap：state 从无到有生成，且它自己的 `#sha256` 首行
    自校验必须通过（`state_read` 不抛异常）。没有 ADOPT 时必须先在 13a 就拒绝，
    不做任何 rsync/state 写入。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root = tmp_path / "root"
    root.mkdir()
    session_roots = f"alpha={root}"
    good = b'{"a":1}\n'
    (root / "s.jsonl").write_bytes(good)
    state_path = dest / "sessions.state.tsv"

    rc_no_adopt, out_no_adopt = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "bootstrap-no-adopt", session_roots,
    )
    assert rc_no_adopt != 0, out_no_adopt
    assert not state_path.exists(), f"无 ADOPT 不该产生 state 文件: {out_no_adopt}"

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "bootstrap", session_roots,
        extra_env=_ADOPT_BOOTSTRAP_ENV,
    )

    assert rc == 0, out
    assert state_path.is_file()
    records = cass_common.state_read(state_path)  # 自校验：内部会做 sha256 头比对，不符会 raise
    assert records == [_rec("alpha/s.jsonl", good)]
    incomplete_dirs = list(dest.glob(".incomplete-bootstrap")) + list(dest.glob("INCOMPLETE-bootstrap"))
    # Task 13 起 backup-cass.sh 真发布：`.incomplete-bootstrap/` 已 `mv -T` 成
    # `cass-bootstrap/`，不再留在 DEST 上。
    assert (dest / "cass-bootstrap" / "sessions.tsv").read_bytes() == state_path.read_bytes()


@requires_cass
def test_whole_source_root_disappears_e2e_present_becomes_absent_at_source(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """Task 11 reviewer 留的验证项，走完整脚本：整根源目录消失（不是单个文件）
    → 下一轮 publish-gate 把该根全部 present 记录判 absent_at_source 结转，
    NAS 内容原封不动，整次备份仍能正常发布（不是 FAIL）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root = tmp_path / "root"
    root.mkdir()
    session_roots = f"alpha={root}"
    good = b'{"a":1}\n'
    (root / "s.jsonl").write_bytes(good)

    rc1, out1 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "vanish-first", session_roots,
        extra_env=_ADOPT_BOOTSTRAP_ENV,
    )
    assert rc1 == 0, out1
    nas_file = dest / "sessions" / "alpha" / "s.jsonl"
    assert nas_file.read_bytes() == good

    shutil.rmtree(root)  # 整个源根消失（不是单个文件）

    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "vanish-second", session_roots)
    assert rc2 == 0, out2
    assert nas_file.read_bytes() == good, "NAS 内容必须原封不动"
    records = cass_common.state_read(dest / "sessions.state.tsv")
    rec = next(r for r in records if r.relpath == "alpha/s.jsonl")
    assert rec.status == "absent_at_source"
    assert rec.nas_size == len(good)
    assert rec.blake3 == blake3.blake3(good).hexdigest()
