"""Tier B acceptance — 真 `cass doctor` 在生产规模镜像上的阳性对照（spec §9.1
V5g，marker `slow realcass`）。

`~/.local/share/coding-agent-search/` 是只读源；本文件在**隔离副本**（`db` +
`raw-mirror/` 两项，不拷贝 `index/`/`vector_index/`/`doctor/`/其它 `*.db.*-bak`
——doctor 的 raw-mirror 校验只依赖这两项，brief 明确要求「cp 目标进 /tmp 或
fixtures 同盘」且只拷贝所需组件，省时间和磁盘）上做「健康基线 → 删 blob →
恢复 → 篡改 blob」三段**串行、恢复式**验证——真 doctor 对 ~2.5GB db + ~2GB
raw-mirror 的单次运行约 5.4 分钟（实测，见下），三次独立副本跑不起，故只建一份
副本，段与段之间显式恢复现场，避免两种破坏叠加互相掩盖。

**经验发现（写在这里，供 codex/人工复核）**：真实生产 db 顶层 `status`/
`reason_code` 因为已知的 `fts_messages_config` 缺陷（spec §2.4/U1）常年是
`unhealthy`/`db_unavailable`——这与 raw-mirror 完全无关，是 db 完整性检查的既有
噪声，不是 Tier 0 门关心的信号。V5g 判据必须扎进 `raw_mirror.status`/
`raw_mirror.summary.*` 子树（`cass_manifest_census.load_doctor_summary` 已经是
这么做的），不能看顶层 `status`。本文件的断言全部只碰 `raw_mirror` 子树，与
Tier A 的 `test_tier0_gate.py`（合成小库，顶层 status 天然 healthy）保持同一套
判据实现，只是跑在真实规模的数据上。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
CENSUS_SCRIPT = REPO / "infra" / "backup" / "cass" / "cass_manifest_census.py"

PROD_DATA_DIR = pathlib.Path.home() / ".local" / "share" / "coding-agent-search"
PROD_DB = PROD_DATA_DIR / "agent_search.db"
PROD_RAW_MIRROR = PROD_DATA_DIR / "raw-mirror"

pytestmark = [pytest.mark.slow, pytest.mark.realcass]


def _manifests_dir(dd: pathlib.Path) -> pathlib.Path:
    return dd / "raw-mirror" / "v1" / "manifests"


def _blobs_root(dd: pathlib.Path) -> pathlib.Path:
    return dd / "raw-mirror" / "v1" / "blobs"


def _run_doctor(dd: pathlib.Path, home: pathlib.Path, out_path: pathlib.Path, timeout: int = 900) -> float:
    """真跑 `cass doctor --json --data-dir <dd>`，stdout 原样落盘，返回耗时秒数。
    **从不看退出码**（同 Tier A test_tier0_gate.py 的既有约定——doctor 对已知
    `fts_messages_config` 缺陷会给出非零 exit，那是已知假阳性，只信 JSON 内容）。
    """
    home.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    result = subprocess.run(
        ["cass", "doctor", "--json", "--data-dir", str(dd)],
        env={"PATH": os.environ["PATH"], "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = time.monotonic() - t0
    out_path.write_text(result.stdout, encoding="utf-8")
    return elapsed


def _raw_mirror_summary(doctor_json_path: pathlib.Path) -> dict:
    doc = json.loads(doctor_json_path.read_text(encoding="utf-8"))
    rm = doc["raw_mirror"]
    assert rm["status"] and "summary" in rm, f"doctor JSON 缺 raw_mirror.status/summary：{doc}"
    return rm


def _census_cli(manifests_dir, doctor_json, blobs_root, timeout=120) -> tuple[int, str, str]:
    cmd = [
        str(VENV_PY), str(CENSUS_SCRIPT),
        "--manifests-dir", str(manifests_dir),
        "--doctor-json", str(doctor_json),
        "--blobs-root", str(blobs_root),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


@pytest.fixture(scope="module")
def isolated_prod_copy(tmp_path_factory):
    """`cp` 生产 `db` + `raw-mirror/`（只读源，不碰生产其它内容）进隔离副本，
    module 级只建一份——三段验证在同一份副本上恢复式进行。"""
    if shutil.which("cass") is None:
        pytest.skip("需要真 cass 二进制")
    if not PROD_DB.is_file():
        pytest.skip(f"生产 db 不存在：{PROD_DB}")
    if not PROD_RAW_MIRROR.is_dir():
        pytest.skip(f"生产 raw-mirror 不存在：{PROD_RAW_MIRROR}")

    dd = tmp_path_factory.mktemp("real-doctor-isolated-dd")
    subprocess.run(["cp", str(PROD_DB), str(dd / "agent_search.db")], check=True, timeout=300)
    subprocess.run(["cp", "-a", str(PROD_RAW_MIRROR), str(dd / "raw-mirror")], check=True, timeout=300)
    return dd


def test_v5g_healthy_then_missing_blob_then_corrupted_blob_recovery_style(isolated_prod_copy, tmp_path):
    dd = isolated_prod_copy
    manifests_dir = _manifests_dir(dd)
    blobs_root = _blobs_root(dd)
    home = tmp_path / "home"

    # ------------------------------------------------------------------
    # 段 1：健康基线 —— raw_mirror.status=verified，五个「必须为0」计数器全0；
    # 独立普查交叉恒等，census CLI PASS。
    # ------------------------------------------------------------------
    doctor_json_1 = tmp_path / "doctor-1-healthy.json"
    elapsed_healthy = _run_doctor(dd, home, doctor_json_1)
    rm1 = _raw_mirror_summary(doctor_json_1)
    assert rm1["status"] == "verified", f"健康基线 raw_mirror.status 应为 verified：{rm1}"
    for key in ("missing_blob_count", "checksum_mismatch_count", "manifest_checksum_mismatch_count",
                "invalid_manifest_count", "interrupted_capture_count"):
        assert rm1["summary"][key] == 0, f"健康基线 {key} 应为 0：{rm1['summary']}"
    assert rm1["summary"]["manifest_count"] > 0, "健康基线应有真实 manifest（生产规模数据）"

    rc1, out1, err1 = _census_cli(manifests_dir, doctor_json_1, blobs_root)
    assert rc1 == 0, f"健康基线 census CLI 应 PASS：\nSTDOUT={out1}\nSTDERR={err1}"
    assert "[PASS]" in out1

    # ------------------------------------------------------------------
    # 段 2：删一个 blob —— doctor 自己报 status!=verified 且 missing_blob_count>0；
    # census CLI FAIL。
    # ------------------------------------------------------------------
    blob_files = sorted(blobs_root.glob("blake3/*/*.raw"))
    assert blob_files, "生产镜像应至少有一个 blob"
    victim = blob_files[0]
    victim_bytes = victim.read_bytes()
    victim.unlink()

    doctor_json_2 = tmp_path / "doctor-2-missing.json"
    _run_doctor(dd, home, doctor_json_2)
    rm2 = _raw_mirror_summary(doctor_json_2)
    assert rm2["status"] != "verified", f"删 blob 后 raw_mirror.status 不应仍为 verified：{rm2}"
    assert rm2["summary"]["missing_blob_count"] > 0, (
        f"阳性对照前提：doctor 自身必须先侦测到缺失，否则本测试没测到目标机制：{rm2['summary']}"
    )

    rc2, out2, err2 = _census_cli(manifests_dir, doctor_json_2, blobs_root)
    assert rc2 == 1, f"删 blob 后 census CLI 应 FAIL：\nSTDOUT={out2}\nSTDERR={err2}"
    assert "Traceback" not in err2, "必须是受控 FAIL，不是裸崩溃"

    # 恢复现场（"恢复式进行"——段3篡改前先把段2删掉的 blob 复原，避免两种破坏叠加
    # 互相掩盖，也避免段3的 doctor 输出同时携带「缺失+篡改」两种噪声）。
    victim.write_bytes(victim_bytes)

    # ------------------------------------------------------------------
    # 段 3：篡改一个 blob（追加字节，尺寸也变，双重确保 checksum 与
    # size 都不一致）—— doctor 报 checksum_mismatch_count>0；census CLI FAIL。
    # ------------------------------------------------------------------
    with open(victim, "ab") as f:
        f.write(b"CASS-TIER-B-ACCEPTANCE-CORRUPTION-MARKER")

    doctor_json_3 = tmp_path / "doctor-3-corrupted.json"
    _run_doctor(dd, home, doctor_json_3)
    rm3 = _raw_mirror_summary(doctor_json_3)
    assert rm3["summary"]["checksum_mismatch_count"] > 0, (
        f"阳性对照前提：doctor 自身必须先侦测到篡改，否则本测试没测到目标机制：{rm3['summary']}"
    )

    rc3, out3, err3 = _census_cli(manifests_dir, doctor_json_3, blobs_root)
    assert rc3 == 1, f"篡改 blob 后 census CLI 应 FAIL：\nSTDOUT={out3}\nSTDERR={err3}"
    assert "Traceback" not in err3, "必须是受控 FAIL，不是裸崩溃"

    print(
        f"\n[V5g timing] healthy doctor run elapsed={elapsed_healthy:.1f}s "
        f"(budget: 900s timeout)"
    )
