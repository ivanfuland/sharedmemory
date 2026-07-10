"""infra/backup/cass/cass_manifest_census.py 的 CLI 端到端测试（Task 8: Tier 0 门 ——
doctor 交叉的独立 manifest 普查，spec §5.6）。

覆盖 Task 8 brief 的 Step 1：

  - 合成 data_dir + 真 doctor 输出 → CLI exit 0（doctor 兼容性在 Task 1 已证）。
  - **V5h（核心）**：手写 doctor JSON 谎称 `verified_blob_count=1`（其余计数器 0、
    `status=verified`），而独立普查（合成 manifests-dir）实有 2 个不同 blob → CLI
    必须 FAIL（三条交叉恒等式不符）。测试内联断言演示：只查 `verified_blob_count>0`
    这类劣化判据会被这份 stub 骗过而误判 PASS——这正是 spec §5.6「零错误与没检查
    在计数器上同形」要独立普查的理由。
  - 删一个 blob 文件（manifests 仍引用，blobs-root 没有）→ stat FAIL。
  - manifest 塞坏 JSON / 缺 `blob_blake3` / 假 hex（64 个 'z'）→ 三种 unparseable
    FAIL，各一例。
  - doctor JSON 缺 summary 键 / `status=warn` → FAIL（doctor 自身不可信/未过判据 1）。
  - manifests-dir 空目录 → FAIL（`manifest_count == 0`）。
  - **真 doctor 阳性对照**（marker `realcass`）：隔离副本删一个 blob →
    doctor 自己报 `missing_blob_count>0` → CLI FAIL；篡改一个 blob 字节 →
    `checksum_mismatch_count>0` → CLI FAIL（探针实证 doctor 对迷你合成镜像秒级）。

全部依赖真 cass 二进制构建 `synth_dd`（marker `realcass` + skipif 缺失时跳过，
与 `test_fixture_factory.py` 同一套约定——`synth_dd` 本身就要真 `cass index`）。
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

pytestmark = [
    pytest.mark.realcass,
    pytest.mark.skipif(
        shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd / 跑真 doctor"
    ),
]


def _manifests_dir(dd: pathlib.Path) -> pathlib.Path:
    return dd / "raw-mirror" / "v1" / "manifests"


def _blobs_root(dd: pathlib.Path) -> pathlib.Path:
    return dd / "raw-mirror" / "v1" / "blobs"


def _run_doctor(dd: pathlib.Path, home: pathlib.Path, out_path: pathlib.Path, timeout: int = 120) -> None:
    """真跑 `cass doctor --json --data-dir <dd>`，stdout 原样落盘。**从不看退出码**
    ——`exit=5` 在健康态也会出现，是已知假阳性（spec 附录 A）；只信 JSON 内容。"""
    result = subprocess.run(
        ["cass", "doctor", "--json", "--data-dir", str(dd)],
        env={"PATH": os.environ["PATH"], "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out_path.write_text(result.stdout, encoding="utf-8")


def _run_cli(manifests_dir, doctor_json, blobs_root) -> tuple[int, str, str]:
    """跑真 CLI 子进程（e2e 覆盖参数解析 / exit code / stdout 指认格式）。"""
    cmd = [
        str(VENV_PY),
        str(CENSUS_SCRIPT),
        "--manifests-dir",
        str(manifests_dir),
        "--doctor-json",
        str(doctor_json),
        "--blobs-root",
        str(blobs_root),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# 健康态：合成 data_dir + 真 doctor 输出 → PASS
# ---------------------------------------------------------------------------


def test_synth_dd_with_real_doctor_passes(tmp_home, synth_dd, tmp_path):
    doctor_json = tmp_path / "doctor.json"
    _run_doctor(synth_dd, tmp_home, doctor_json)

    rc, out, err = _run_cli(_manifests_dir(synth_dd), doctor_json, _blobs_root(synth_dd))

    assert rc == 0, f"stdout={out}\nstderr={err}"
    assert "[PASS]" in out


# ---------------------------------------------------------------------------
# V5h（核心）：stub doctor 谎称 verified_blob_count=1，而普查实有 2 个不同 blob
# ---------------------------------------------------------------------------


def test_v5h_identity_mismatch_catches_stub_doctor_that_only_checks_gt_zero(synth_dd, tmp_path):
    manifests_out = tmp_path / "manifests"
    blobs_out = tmp_path / "blobs"
    shutil.copytree(_manifests_dir(synth_dd), manifests_out)
    shutil.copytree(_blobs_root(synth_dd), blobs_out)

    # 手造第二对 manifest+blob（内容任意 nonsense；只喂本 CLI，不喂 doctor——
    # brief 明确授权的合成夹具边界）。真实 synth_dd 的 2 份 manifest 恰好指向同一个
    # blob（unique_blobs=1），加这一份后凑成 2 个不同 blob，3 个 manifest。
    extra_blob_bytes = b"lorem-v5h-synthetic-second-blob-nonsense"
    extra_hash = blake3.blake3(extra_blob_bytes).hexdigest()
    extra_blob_path = blobs_out / "blake3" / extra_hash[:2] / f"{extra_hash}.raw"
    extra_blob_path.parent.mkdir(parents=True, exist_ok=True)
    extra_blob_path.write_bytes(extra_blob_bytes)

    extra_manifest = {
        "schema_version": 1,
        "manifest_kind": "cass_raw_session_mirror_v1",
        "manifest_id": "doctor-raw-mirror-manifest-id-v1-synthetic-v5h",
        "blob_hash_algorithm": "blake3",
        "blob_relative_path": f"blobs/blake3/{extra_hash[:2]}/{extra_hash}.raw",
        "blob_blake3": extra_hash,
        "blob_size_bytes": len(extra_blob_bytes),
    }
    (manifests_out / "synthetic-v5h-manifest.json").write_text(
        json.dumps(extra_manifest), encoding="utf-8"
    )

    # 独立普查真值（直接调 Python 函数验证夹具确实构造出「≥2 个不同 blob」）：
    census, _ = cass_manifest_census.census_manifests(manifests_out)
    assert census.manifest_count == 3
    assert census.unique_blobs == 2
    assert census.duplicate_refs == 1
    assert census.unparseable == 0

    # stub doctor：谎称 manifest_count=1、verified_blob_count=1、duplicate=0，
    # 五计数器全 0、status=verified。
    stub_summary = {
        "manifest_count": 1,
        "verified_blob_count": 1,
        "missing_blob_count": 0,
        "checksum_mismatch_count": 0,
        "manifest_checksum_mismatch_count": 0,
        "invalid_manifest_count": 0,
        "interrupted_capture_count": 0,
        "duplicate_blob_reference_count": 0,
    }
    # 对照断言：只查 `verified_blob_count > 0` 这类劣化判据会被这份 stub 骗过而误判
    # PASS——这正是 spec §5.6 要独立普查交叉验证、而不能只信 doctor 自述计数器的理由。
    assert stub_summary["verified_blob_count"] > 0

    doctor_json = tmp_path / "doctor.json"
    doctor_json.write_text(
        json.dumps({"raw_mirror": {"status": "verified", "summary": stub_summary}}),
        encoding="utf-8",
    )

    rc, out, err = _run_cli(manifests_out, doctor_json, blobs_out)

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "恒等式不符" in out


# ---------------------------------------------------------------------------
# blob 文件缺失 → stat FAIL（doctor JSON 本身不变，隔离判据 4）
# ---------------------------------------------------------------------------


def test_missing_blob_file_triggers_stat_fail(tmp_home, synth_dd, tmp_path):
    doctor_json = tmp_path / "doctor.json"
    _run_doctor(synth_dd, tmp_home, doctor_json)

    blobs_root = _blobs_root(synth_dd)
    blob_files = list(blobs_root.glob("blake3/*/*.raw"))
    assert blob_files, "synth_dd 应至少有一个 blob"
    blob_files[0].unlink()

    rc, out, err = _run_cli(_manifests_dir(synth_dd), doctor_json, blobs_root)

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "blob 文件缺失" in out


# ---------------------------------------------------------------------------
# 普查自身 fail-loud：坏 JSON / 缺 blob_blake3 / 假 hex，各一例
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "corrupt_fn,expected_substr",
    [
        pytest.param(
            lambda p: p.write_text("{not valid json", encoding="utf-8"),
            "JSON 解析失败",
            id="bad-json",
        ),
        pytest.param(
            lambda p: p.write_text(json.dumps({"schema_version": 1}), encoding="utf-8"),
            "blob_blake3 缺失或不匹配",
            id="missing-blob-blake3-key",
        ),
        pytest.param(
            lambda p: p.write_text(json.dumps({"blob_blake3": "z" * 64}), encoding="utf-8"),
            "blob_blake3 缺失或不匹配",
            id="fake-hex",
        ),
    ],
)
def test_unparseable_manifest_variants_fail_loud(
    tmp_home, synth_dd, tmp_path, corrupt_fn, expected_substr
):
    doctor_json = tmp_path / "doctor.json"
    _run_doctor(synth_dd, tmp_home, doctor_json)

    manifests_out = tmp_path / "manifests"
    shutil.copytree(_manifests_dir(synth_dd), manifests_out)
    corrupt_target = sorted(manifests_out.glob("*.json"))[0]
    corrupt_fn(corrupt_target)

    rc, out, err = _run_cli(manifests_out, doctor_json, _blobs_root(synth_dd))

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "unparseable=1" in out
    assert expected_substr in out


# ---------------------------------------------------------------------------
# doctor JSON 不可信：缺 summary 键 / status=warn → FAIL
# ---------------------------------------------------------------------------


def test_doctor_missing_summary_key_fails(tmp_home, synth_dd, tmp_path):
    doctor_json = tmp_path / "doctor.json"
    _run_doctor(synth_dd, tmp_home, doctor_json)
    doc = json.loads(doctor_json.read_text(encoding="utf-8"))
    del doc["raw_mirror"]["summary"]["missing_blob_count"]
    doctor_json.write_text(json.dumps(doc), encoding="utf-8")

    rc, out, err = _run_cli(_manifests_dir(synth_dd), doctor_json, _blobs_root(synth_dd))

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "doctor JSON 不可信" in out
    assert "missing_blob_count" in out


def test_doctor_status_warn_fails(tmp_home, synth_dd, tmp_path):
    doctor_json = tmp_path / "doctor.json"
    _run_doctor(synth_dd, tmp_home, doctor_json)
    doc = json.loads(doctor_json.read_text(encoding="utf-8"))
    doc["raw_mirror"]["status"] = "warn"
    doctor_json.write_text(json.dumps(doc), encoding="utf-8")

    rc, out, err = _run_cli(_manifests_dir(synth_dd), doctor_json, _blobs_root(synth_dd))

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "status='warn'" in out


# ---------------------------------------------------------------------------
# manifests-dir 空目录 → FAIL（manifest_count == 0）
# ---------------------------------------------------------------------------


def test_empty_manifests_dir_fails(tmp_home, synth_dd, tmp_path):
    doctor_json = tmp_path / "doctor.json"
    _run_doctor(synth_dd, tmp_home, doctor_json)
    empty_dir = tmp_path / "empty-manifests"
    empty_dir.mkdir()

    rc, out, err = _run_cli(empty_dir, doctor_json, _blobs_root(synth_dd))

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "manifest_count == 0" in out


# ---------------------------------------------------------------------------
# 真 doctor 阳性对照：隔离副本删 blob / 篡改 blob，doctor 自己报出异常 → CLI FAIL
# ---------------------------------------------------------------------------


def test_real_doctor_positive_control_deleted_blob_fails(tmp_home, synth_dd, tmp_path):
    blobs_root = _blobs_root(synth_dd)
    blob_files = list(blobs_root.glob("blake3/*/*.raw"))
    assert blob_files, "synth_dd 应至少有一个 blob"
    blob_files[0].unlink()

    doctor_json = tmp_path / "doctor.json"
    _run_doctor(synth_dd, tmp_home, doctor_json)

    doc = json.loads(doctor_json.read_text(encoding="utf-8"))
    assert doc["raw_mirror"]["summary"]["missing_blob_count"] > 0, (
        "阳性对照前提：doctor 自身必须先侦测到缺失，否则本测试没测到目标机制"
    )

    rc, out, err = _run_cli(_manifests_dir(synth_dd), doctor_json, blobs_root)

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "Traceback" not in err, "必须是受控 FAIL，不是裸崩溃"
    assert "[FAIL]" in out
    assert "missing_blob_count" in out


def test_real_doctor_positive_control_corrupted_blob_fails(tmp_home, synth_dd, tmp_path):
    blobs_root = _blobs_root(synth_dd)
    blob_files = list(blobs_root.glob("blake3/*/*.raw"))
    assert blob_files, "synth_dd 应至少有一个 blob"
    with open(blob_files[0], "ab") as f:
        f.write(b"CORRUPTED")

    doctor_json = tmp_path / "doctor.json"
    _run_doctor(synth_dd, tmp_home, doctor_json)

    doc = json.loads(doctor_json.read_text(encoding="utf-8"))
    assert doc["raw_mirror"]["summary"]["checksum_mismatch_count"] > 0, (
        "阳性对照前提：doctor 自身必须先侦测到篡改，否则本测试没测到目标机制"
    )

    rc, out, err = _run_cli(_manifests_dir(synth_dd), doctor_json, blobs_root)

    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "Traceback" not in err, "必须是受控 FAIL，不是裸崩溃"
    assert "[FAIL]" in out
    assert "checksum_mismatch_count" in out
