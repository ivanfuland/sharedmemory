"""`infra/backup/backup-cass.sh` 首晚 ADOPT bootstrap 的端到端聚合测试（Task 17：
首晚 bootstrap 端到端 + Tier A 全绿收口，P8 显式路径）。

首晚 ADOPT/generation/链头/腿 3-4 登记模式这些语义此前分散在
`test_sessions_state.py`（sessions 通道视角）、`test_publish.py`（digest.json
字段视角）、`test_chain.py`（链校验视角）——各自只覆盖自己关心的那一面。本文件
把它们聚合成一组连续的端到端断言（同一个 DEST，跨三晚），把「首晚到底发生了
什么」钉成一份可以从头读到尾的证据链：

  1. 首晚三连：全新 DEST + `CASS_BACKUP_ADOPT_SESSIONS=1`+reason → 发布
     `cass-*/`：`generation==1`、`prev_backup_name`/`prev_sidecar_sha256` 均为
     空串、digest.json 含 `adopt_reason`；腿 3/4 登记模式证据——census.tsv 有
     真实值、digest.json 折进的 `tables`/`schema_fingerprint`/`meta_watermarks`
     非空（`gate.json` 本身只是 staging 产物，PASS 路径不落 NAS，它的字段经
     backup-cass.sh step 14c 原样折进 digest.json，见该脚本 ~L650-668）；
     `verify_chain(dest, keep)` PASS（首次基线链头合法）；`sessions.state.tsv`
     存在且首行 `#sha256` 自校验通过。
  2. 第二晚挂钩：正常跑（不带 ADOPT——state 已存在）→ generation 2、prev 指针/
     sha 正确指向首晚；然后在源 db 上注入攻击⑥（`cass_backup_gate.leg4` 水位
     单调性判据，`meta.last_scan_ts` 改小）再跑第三晚 → exit 非零 + `SUSPECT-*/`
     落地，`[leg 4] FAIL` 且报文指名 `last_scan_ts`——证明基线比对已经真的挂钩
     到首晚发布的 digest.json（不是每晚都退化成首晚的登记模式放行）。
  3. 缺 ADOPT 的首晚（独立、干净 DEST）→ exit 非零、stdout 指认
     `sessions.state.tsv missing` + 需要 `ADOPT`、无任何发布产物（无 `cass-*/`、
     无 `sessions.state.tsv`——`INCOMPLETE-*/` 取证半成品不算「发布产物」，这是
     spec §6.3.1 step 13a 的既有语义，不是本文件新断言，见
     `test_sessions_state.py::test_first_night_adopt_bootstrap_state_generated_and_self_verifies_e2e`
     的同款前半段）。

本文件自包含，不跨文件 import 其它测试文件的私有函数（同代码库既有约定）；
`_run`/`_write_verified_doctor_stub` 逐字搬自 `test_publish.py` 同名 helper。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil

import pytest

import cass_chain
import cass_common
import fixture_factory

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "infra" / "backup" / "backup-cass.sh"

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd"
)


# ---------------------------------------------------------------------------
# 帮手（本文件自包含，不跨文件 import 其它测试文件的私有函数——同代码库既有约定）。
# ---------------------------------------------------------------------------


def _write_verified_doctor_stub(home: pathlib.Path, manifests_dir: pathlib.Path) -> None:
    """同其它 task 文件的写法——Tier 0 门必须先 PASS 才能走到 step 9+ 的五腿门。"""
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
    """跑一次 `backup-cass.sh`，固定 stamp。**不**像 `test_publish.py` 的同名
    helper 那样默认注入 ADOPT——本文件的核心断言正是「有/无 ADOPT 分别发生什么」，
    调用方按需显式传 `_ADOPT_ENV`。"""
    _write_verified_doctor_stub(tmp_home, synth_dd / "raw-mirror" / "v1" / "manifests")
    env = {
        "CASS_DATA_DIR": str(synth_dd),
        "CASS_BACKUP_DEST": str(dest),
        "CASS_BACKUP_STAGING": str(staging),
        "CASS_BACKUP_STAMP": stamp,
        "PATH": f"{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    if extra_env:
        env.update(extra_env)
    rc, out, _dest = run_backup(env=env)
    return rc, out


_ADOPT_REASON = "test fixture — first-night bootstrap e2e (Task 17)"
_ADOPT_ENV = {
    "CASS_BACKUP_ADOPT_SESSIONS": "1",
    "CASS_BACKUP_ADOPT_REASON": _ADOPT_REASON,
}


def _census_dict(path: pathlib.Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    return dict(line.split("\t", 1) for line in text.splitlines() if line)


# ---------------------------------------------------------------------------
# 首晚三连 + 第二晚挂钩（同一 DEST，跨三晚）
# ---------------------------------------------------------------------------


@requires_cass
def test_first_night_bootstrap_and_baseline_hookup_e2e(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    # ---- night 1: 首晚三连 ----
    rc1, out1 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "fn-night1",
        extra_env=_ADOPT_ENV,
    )
    assert rc1 == 0, out1

    night1_dir = dest / "cass-fn-night1"
    assert night1_dir.is_dir(), out1
    assert (night1_dir / "COMPLETE").is_file(), out1

    digest1 = json.loads((night1_dir / "digest.json").read_bytes())
    assert digest1["backup_name"] == "cass-fn-night1"
    assert digest1["generation"] == 1
    assert digest1["prev_backup_name"] == ""
    assert digest1["prev_sidecar_sha256"] == ""
    assert digest1["adopt_reason"] == _ADOPT_REASON

    # 腿 3/4 登记模式证据：census.tsv 是真实普查值（不是首晚跳过留空）。
    census1 = _census_dict(night1_dir / "census.tsv")
    assert int(census1["messages"]) > 0, census1
    assert int(census1["conversations"]) > 0, census1
    # gate.json 是 staging 产物（PASS 路径不落 NAS），它的字段折进 digest.json——
    # 非空即证明腿 3/4 真的算出了指纹/摘要/水位，不是首晚被静默跳过。
    assert digest1["schema_fingerprint"], "首晚登记模式仍应算出非空 schema 指纹"
    assert digest1["tables"], "首晚登记模式仍应记录 tables 摘要"
    assert digest1["meta_watermarks"], "首晚登记模式仍应记录水位"

    # 链校验：首次基线链头合法（单份、generation 1、prev 全空）。
    assert cass_chain.verify_chain(dest, keep=7) == []

    # sessions.state.tsv：存在且首行 #sha256 自校验通过（state_read 内部核对，
    # 不符会抛 StateCorrupt——这里不抛异常本身就是断言）。
    state_path = dest / "sessions.state.tsv"
    assert state_path.is_file()
    cass_common.state_read(state_path)

    # ---- night 2: 正常挂钩（不带 ADOPT——state 已存在，不需要它） ----
    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "fn-night2")
    assert rc2 == 0, out2

    night2_dir = dest / "cass-fn-night2"
    assert night2_dir.is_dir(), out2
    digest2 = json.loads((night2_dir / "digest.json").read_bytes())
    assert digest2["generation"] == 2
    assert digest2["prev_backup_name"] == "cass-fn-night1"
    assert digest2["prev_sidecar_sha256"] == cass_common.sha256_file(
        night1_dir / "digest.json"
    )
    assert "adopt_reason" not in digest2, "第二晚没有再传 ADOPT，不该留痕"
    assert cass_chain.verify_chain(dest, keep=7) == []

    # ---- night 3: 注入攻击⑥（meta.last_scan_ts 回退）——证明基线比对已挂钩到
    # 首晚发布的 digest.json，不是每晚都退化成登记模式放行。 ----
    fixture_factory.attack6(synth_dd / "agent_search.db")
    rc3, out3 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "fn-night3")

    assert rc3 != 0, out3
    assert "[leg 4] FAIL" in out3, out3
    assert "last_scan_ts" in out3, out3

    suspect_dir = dest / "SUSPECT-fn-night3"
    assert suspect_dir.is_dir(), out3
    assert (suspect_dir / "digest.json").is_file()
    suspect_digest = json.loads((suspect_dir / "digest.json").read_bytes())
    assert suspect_digest["generation"] == 3
    assert suspect_digest["prev_backup_name"] == "cass-fn-night2"

    # SUSPECT 是独立取证目录，不覆盖/影响已发布的 cass-*/。
    assert night1_dir.is_dir() and (night1_dir / "COMPLETE").is_file()
    assert night2_dir.is_dir() and (night2_dir / "COMPLETE").is_file()
    assert not (dest / "cass-fn-night3").exists(), "五腿门 FAIL 不该发布 cass-*/"


# ---------------------------------------------------------------------------
# 缺 ADOPT 的首晚（独立、干净 DEST）
# ---------------------------------------------------------------------------


@requires_cass
def test_first_night_without_adopt_fails_closed_e2e(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "fn-no-adopt")

    assert rc != 0, out
    assert "sessions.state.tsv missing" in out, out
    assert "ADOPT" in out, out
    assert not (dest / "sessions.state.tsv").exists(), (
        f"无 ADOPT 不该产生 state 文件: {out}"
    )
    assert not list(dest.glob("cass-*")), (
        f"无 ADOPT 不该有任何发布成功的 cass-*/: {out}"
    )
