"""`infra/backup/cass/cass_weekly.py` + `backup-cass.sh` step 18 的测试（Task 16：
周深度校验，spec §6.5，数据流 step 18）。

覆盖 task-16-brief 的场景（`verify_weekly(dest, keep) -> list[str]`，空=PASS）：

  - 健康两晚真备份 → `verify_weekly` PASS；CLI `--dest`/`--keep` exit 0，stdout
    含 `[weekly] PASS`。
  - V14a：造一个「只被旧备份（night1）manifest 引用、当前源端已无」的 blob，
    在 NAS 池里篡改它（保持同长度）→ 全池 blake3 重算（①）检出；两个反例断言
    内联演示：`rsync --checksum --dry-run`（源端已无该文件，看不到任何变化行）
    与本模块自己的 `verify_closure`（②，只 `stat` 存在性、不重算内容）都检不出。
  - V14b：孤儿 blob（NAS 池里存在、但没有任何保留 manifest 引用过）篡改 → 只有
    全池扫描（①）能抓；反例断言 `verify_closure` 对它天然视而不见（它只遍历
    manifest 引用，压根不知道这个文件存在）。
  - V14c：monkeypatch `os.posix_fadvise` 计数——健康两晚状态下调用数必须恰好
    等于「blob 池全部 + 每个保留备份的 db/census.tsv/sessions.tsv/manifests.sha256sum
    自身各一次 + 每份 manifest 一次 + sessions.state.tsv 一次」（覆盖集断言，逐
    文件恰一次；manifests.sha256sum 自身一次是 codex R1 P1-1 新增的 digest 绑定
    读，3×retained 变 4×retained）。blob 期望数用 `rglob("*.raw")` 独立推导（不
    照抄生产 glob），另断言两种口径相等——生产 glob 若静默变窄，等式两边同降的
    假绿就此被拦。
  - V15m：篡改某保留 `cass-*/db` 一个字节 → `verify_weekly` FAIL 报文含
    `db: FAILED`；反例断言：只调用 `verify_blob_pool`（①）+
    `cass_chain.verify_chain`（③）时两者全过——db 内容不是 blob，也不在链算法
    的比对范围内，只有 ④ 的自校验能抓。
  - digest.json 缺字段 / 坏 JSON / 单文件 `chmod 000`（Task 14 review 留意项：
    确认周通道整体 FAIL，不 skip，与链校验同一场景的判定一致）三种保留目录
    损坏 → 均 FAIL。
  - `sessions.state.tsv` 首行篡改 → FAIL（⑤）。
  - CLI 对空 DEST（无任何已发布备份）exit 1，stdout 含 `[weekly] FAIL`。
  - bash 层（`backup-cass.sh` step 18）：`CASS_BACKUP_VERIFY_DOW=$(date +%u)`
    时——① 健康态：stdout 含 weekly 标记 + exit 0；② 注入孤儿 blob 篡改（不影响
    nightly 各门，只有周校验能抓）：exit 非零 + stdout 含 `[weekly] FAIL` +
    备份本身仍已发布（`COMPLETE` 仍在）；`VERIFY_DOW` 设为明天（`dow%7+1`）→
    stdout 含 `weekly verify: skipped`、不出现 `[weekly]`（周校验 CLI 根本没被
    调用）、exit 0。

本文件自包含，不跨文件 import 其它测试文件的私有函数（同代码库既有约定）。
大多数测试依赖真 `cass` 二进制构建 `synth_dd`（`requires_cass`）。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import blake3
import pytest

import cass_chain
import cass_common
import cass_manifest_census
import cass_weekly

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
WEEKLY_SCRIPT = REPO / "infra" / "backup" / "cass" / "cass_weekly.py"

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd"
)


# ---------------------------------------------------------------------------
# 帮手（本文件自包含，不跨文件 import 其它测试文件的私有函数——同代码库既有约定）。
# ---------------------------------------------------------------------------


def _write_verified_doctor_stub(home: pathlib.Path, manifests_dir: pathlib.Path) -> None:
    """同其它 task 文件的写法——Tier 0 门必须先 PASS 才能走到 step 10+。"""
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
    一份 ADOPT bootstrap env——本文件的测试关注周校验，与 sessions 通道本身正交，
    同其它 task 文件的 `_run` 约定。"""
    _write_verified_doctor_stub(tmp_home, synth_dd / "raw-mirror" / "v1" / "manifests")
    env = {
        "CASS_DATA_DIR": str(synth_dd),
        "CASS_BACKUP_DEST": str(dest),
        "CASS_BACKUP_STAGING": str(staging),
        "CASS_BACKUP_STAMP": stamp,
        "CASS_BACKUP_ADOPT_SESSIONS": "1",
        "CASS_BACKUP_ADOPT_REASON": "test fixture — weekly verify not sessions channel",
        "PATH": f"{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    if extra_env:
        env.update(extra_env)
    rc, out, _dest = run_backup(env=env)
    return rc, out


def _retained_backup_dirs(dest: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in dest.glob("cass-*") if p.is_dir() and (p / "COMPLETE").is_file())


def _write_manifest_blob(
    manifests_dir: pathlib.Path, blobs_root: pathlib.Path, content: bytes, manifest_id: str
) -> str:
    """在 `manifests_dir`/`blobs_root` 下写一份真实自洽的 manifest+blob 对（同
    `test_blobs_manifests.py::test_v13a2_...` 的手法）。返回 blob 的 blake3 hex。"""
    h = blake3.blake3(content).hexdigest()
    blob_path = blobs_root / "blake3" / h[:2] / f"{h}.raw"
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(content)
    manifest = {
        "schema_version": 1,
        "manifest_kind": "cass_raw_session_mirror_v1",
        "manifest_id": manifest_id,
        "blob_hash_algorithm": "blake3",
        "blob_relative_path": f"blobs/blake3/{h[:2]}/{h}.raw",
        "blob_blake3": h,
        "blob_size_bytes": len(content),
    }
    (manifests_dir / f"{manifest_id}.json").write_text(json.dumps(manifest), encoding="utf-8")
    return h


def _cli(dest, keep) -> tuple[int, str, str]:
    result = subprocess.run(
        [str(VENV_PY), str(WEEKLY_SCRIPT), "--dest", str(dest), "--keep", str(keep)],
        capture_output=True, text=True, timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# 健康两晚真备份：verify_weekly PASS + CLI exit 0
# ---------------------------------------------------------------------------


@requires_cass
def test_healthy_two_night_backup_weekly_verify_passes(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "healthy-night1")
    assert rc1 == 0, out1
    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "healthy-night2")
    assert rc2 == 0, out2

    problems = cass_weekly.verify_weekly(dest, keep=7)
    assert problems == [], problems

    rc, out, err = _cli(dest, 7)
    assert rc == 0, f"stdout={out}\nstderr={err}"
    assert "[weekly] PASS" in out


# ---------------------------------------------------------------------------
# codex R1 P1-1：一致替换某保留 manifest + 同步重新生成 manifests.sha256sum ——
# 只有绑定 digest.json.manifests_sha256sum_sha256 的锚点检查能抓出
# ---------------------------------------------------------------------------


@requires_cass
def test_p1_1_manifest_and_sha256sum_both_replaced_consistently_caught_by_digest_binding(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """把某个保留 `cass-*/manifests/` 里一份 manifest 整体换成另一份真实自洽
    manifest（引用一个真实存在、内容匹配的 blob），并**同步重新生成**
    `manifests.sha256sum`（保持"manifest 内容与其记录的哈希彼此自洽"）——单独看
    `verify_manifests_sha256sum` 这一步会 PASS（两者一起换，内部自洽性没被破坏，
    同 test_blobs_manifests.py::test_v13a2 的手法，但这里是发布**之后**的保留目录
    ，不是发布前的 `.incomplete` 暂存区）。只有 `manifests.sha256sum` 自身的
    sha256 对 `digest.json.manifests_sha256sum_sha256`（发布当晚记录的锚点值，
    发布后不可变）的绑定检查能抓出这种「整体调包 + 同步重算校验文件」的攻击
    （spec §11 硬约束：保留 cass-*/ 的自校验含 manifests.sha256sum 对 digest.json）。
    """
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "p1-1")
    assert rc == 0, out

    # 正常路径基线：篡改前 weekly 必须 PASS（对照，避免下面的 FAIL 只是别的原因）。
    assert cass_weekly.verify_weekly(dest, keep=7) == []

    backup_dir = dest / "cass-p1-1"
    manifests_dir = backup_dir / "manifests"
    blobs_root = dest / "raw-mirror" / "v1" / "blobs"

    # 造一份真实自洽的替换 manifest（同 test_v13a2 手法）。
    extra_content = b"weekly-p1-1-self-consistent-alternate-manifest-blob"
    extra_hash = blake3.blake3(extra_content).hexdigest()
    extra_blob_path = blobs_root / "blake3" / extra_hash[:2] / f"{extra_hash}.raw"
    extra_blob_path.parent.mkdir(parents=True, exist_ok=True)
    extra_blob_path.write_bytes(extra_content)

    replacement_manifest = {
        "schema_version": 1,
        "manifest_kind": "cass_raw_session_mirror_v1",
        "manifest_id": "p1-1-synthetic-replacement",
        "blob_hash_algorithm": "blake3",
        "blob_relative_path": f"blobs/blake3/{extra_hash[:2]}/{extra_hash}.raw",
        "blob_blake3": extra_hash,
        "blob_size_bytes": len(extra_content),
    }
    target = sorted(manifests_dir.glob("*.json"))[0]
    target.write_text(json.dumps(replacement_manifest), encoding="utf-8")

    # 攻击的关键一步：同步重新生成 manifests.sha256sum，让「manifest 内容 vs
    # manifests.sha256sum」这组自洽性检查（verify_manifests_sha256sum）看不出问题。
    subprocess.run(
        "sha256sum manifests/*.json > manifests.sha256sum",
        shell=True, cwd=backup_dir, check=True, timeout=30,
    )

    # 反例断言：单独看 verify_manifests_sha256sum，两者一起换后确实自洽、PASS。
    ok_self_consistency, self_consistency_problems = cass_manifest_census.verify_manifests_sha256sum(
        manifests_dir, backup_dir / "manifests.sha256sum"
    )
    assert ok_self_consistency, (
        f"反例应证伪：manifest 与重新生成的 manifests.sha256sum 彼此自洽: {self_consistency_problems}"
    )

    problems = cass_weekly.verify_weekly(dest, keep=7)
    assert any("manifests.sha256sum" in p and "FAILED" in p for p in problems), problems


# ---------------------------------------------------------------------------
# V14a：blob 只被旧备份（night1）manifest 引用、当前源端已无 → 全池扫描检出
# ---------------------------------------------------------------------------


@requires_cass
def test_v14a_blob_only_referenced_by_old_backup_manifest_bitrot_caught_by_full_pool_scan(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    manifests_dir = synth_dd / "raw-mirror" / "v1" / "manifests"
    blobs_root_source = synth_dd / "raw-mirror" / "v1" / "blobs"

    # 造第二份内容（h2）——night1 会同时引用 build_data_dir 自带的 blob（h1）与
    # h2，让 night2 在 h1 从源端消失后仍有内容可备份。
    h2 = _write_manifest_blob(
        manifests_dir, blobs_root_source, b"v14a-second-blob-still-at-source", "v14a-m2"
    )

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v14a-night1")
    assert rc1 == 0, out1

    night1_manifests_dir = dest / "cass-v14a-night1" / "manifests"
    h1 = None
    for m in night1_manifests_dir.glob("*.json"):
        data = json.loads(m.read_text(encoding="utf-8"))
        if data["blob_blake3"] != h2:
            h1 = data["blob_blake3"]
            break
    assert h1 is not None, "night1 应该引用了 build_data_dir 自带的那个 blob"

    nas_blobs_root = dest / "raw-mirror" / "v1" / "blobs"
    h1_pool_path = cass_manifest_census.blob_path_for(nas_blobs_root, h1)
    assert h1_pool_path.is_file(), "night1 应已把 h1 同步进共享池"

    # 模拟源端 dedup/vacuum：删掉源端所有引用 h1 的 manifest（build_data_dir 会
    # 产出 2 份重复引用同一 blob 的 manifest——都要删掉）+ 源端的 blob 本体，
    # 只留 v14a-m2。
    for m in list(manifests_dir.glob("*.json")):
        data = json.loads(m.read_text(encoding="utf-8"))
        if data["blob_blake3"] == h1:
            m.unlink()
    source_h1_blob = blobs_root_source / "blake3" / h1[:2] / f"{h1}.raw"
    if source_h1_blob.is_file():
        source_h1_blob.unlink()

    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v14a-night2")
    assert rc2 == 0, out2

    night2_manifests_dir = dest / "cass-v14a-night2" / "manifests"
    night2_hashes = {
        json.loads(m.read_text(encoding="utf-8"))["blob_blake3"]
        for m in night2_manifests_dir.glob("*.json")
    }
    assert h1 not in night2_hashes, "night2 的 manifest 快照不应再引用 h1（源端已无）"

    # 篡改 NAS 池里的 h1，保持同长度——内容寻址判据，不是 st_size。
    original = h1_pool_path.read_bytes()
    corrupted = bytearray(original)
    corrupted[0] ^= 0xFF
    h1_pool_path.write_bytes(bytes(corrupted))

    # 反例断言 1：rsync --checksum --dry-run（源端已无该文件）看不到任何变化行。
    dry_run = subprocess.run(
        ["rsync", "-a", "--checksum", "--itemize-changes", "--dry-run",
         f"{blobs_root_source}/", f"{nas_blobs_root}/"],
        capture_output=True, text=True, timeout=30,
    )
    assert h1 not in dry_run.stdout, (
        f"反例应证伪：rsync --checksum --dry-run 对源端已无的文件应看不到任何变化行: {dry_run.stdout}"
    )

    # 反例断言 2：闭合检查（②）只 stat 存在性，不重算内容——对这处腐烂视而不见
    # （h1 仍被 night1 的（不可变）manifest 快照引用，闭合检查会去 stat 它，
    # 文件存在、size 不变，判定通过）。
    retained = _retained_backup_dirs(dest)
    closure_problems = cass_weekly.verify_closure(retained, nas_blobs_root)
    assert closure_problems == [], (
        f"反例应证伪：只 stat 存在性的闭合检查抓不住内容篡改: {closure_problems}"
    )

    pool_problems = cass_weekly.verify_blob_pool(nas_blobs_root)
    assert any(str(h1_pool_path) in p for p in pool_problems), pool_problems

    full_problems = cass_weekly.verify_weekly(dest, keep=7)
    assert any(str(h1_pool_path) in p for p in full_problems), full_problems


# ---------------------------------------------------------------------------
# V14b：孤儿 blob（无任何保留 manifest 引用）→ 只有全池扫描能抓
# ---------------------------------------------------------------------------


@requires_cass
def test_v14b_orphan_blob_never_referenced_bitrot_caught_only_by_full_pool_scan(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v14b")
    assert rc == 0, out

    nas_blobs_root = dest / "raw-mirror" / "v1" / "blobs"
    orphan_content = b"v14b-orphan-blob-never-referenced-by-any-manifest"
    orphan_hash = blake3.blake3(orphan_content).hexdigest()
    orphan_path = nas_blobs_root / "blake3" / orphan_hash[:2] / f"{orphan_hash}.raw"
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_bytes(orphan_content)

    # 先证明它确实是孤儿——没有任何保留 manifest 引用它。
    for backup_dir in _retained_backup_dirs(dest):
        for manifest in (backup_dir / "manifests").glob("*.json"):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            assert data.get("blob_blake3") != orphan_hash

    # 篡改（保持同长度）。
    corrupted = bytearray(orphan_content)
    corrupted[0] ^= 0xFF
    orphan_path.write_bytes(bytes(corrupted))

    retained = _retained_backup_dirs(dest)
    closure_problems = cass_weekly.verify_closure(retained, nas_blobs_root)
    assert closure_problems == [], (
        f"孤儿 blob 不被任何保留 manifest 引用，闭合检查天然够不到它: {closure_problems}"
    )

    pool_problems = cass_weekly.verify_blob_pool(nas_blobs_root)
    assert any(str(orphan_path) in p for p in pool_problems), pool_problems

    full_problems = cass_weekly.verify_weekly(dest, keep=7)
    assert any(str(orphan_path) in p for p in full_problems), full_problems


# ---------------------------------------------------------------------------
# V14c：fadvise 覆盖集——blob 全部 + 每个保留备份 db/manifest*/census.tsv/
# sessions.tsv 各恰一次 + sessions.state.tsv 一次。
# ---------------------------------------------------------------------------


@requires_cass
def test_v14c_fadvise_called_once_per_covered_nas_file(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path, monkeypatch
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v14c-night1")
    assert rc1 == 0, out1
    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v14c-night2")
    assert rc2 == 0, out2

    calls: list[int] = []
    real_fadvise = os.posix_fadvise

    def _spy(fd, offset, length, advice):
        calls.append(fd)
        return real_fadvise(fd, offset, length, advice)

    monkeypatch.setattr(os, "posix_fadvise", _spy)

    problems = cass_weekly.verify_weekly(dest, keep=7)
    assert problems == [], problems

    blobs_root = dest / "raw-mirror" / "v1" / "blobs"
    # blob 期望数独立推导（rglob，深度无关）而非照抄生产 glob `blake3/*/*.raw`
    # ——生产 glob 若静默变窄（漏扫一层目录），等式两边同降会假绿。先断言两种
    # 口径当下相等：未来不等即报警（要么池布局变了，要么生产 glob 真的漏了）。
    n_blobs = len(list(blobs_root.rglob("*.raw")))
    n_blobs_production_glob = len(list(blobs_root.glob("blake3/*/*.raw")))
    assert n_blobs == n_blobs_production_glob, (
        f"blob 计数口径分歧：rglob={n_blobs} vs 生产 glob={n_blobs_production_glob}"
        "——池布局变化或生产 glob 变窄，先查清再改期望"
    )
    assert n_blobs > 0, "覆盖集断言至少要有 1 个 blob 才有意义"
    retained = _retained_backup_dirs(dest)
    n_manifests = sum(len(list((b / "manifests").glob("*.json"))) for b in retained)
    # 每个保留备份各读一次 db + census.tsv + sessions.tsv + manifests.sha256sum
    # 自身（P1-1 的 digest 绑定读——见 verify_backup_self）。
    n_per_backup_files = len(retained) * 4
    # sessions.state.tsv 一次（⑤ 读前同样 fadvise——页缓存陈旧性与文件大小无关）。
    n_state = 1

    expected = n_blobs + n_manifests + n_per_backup_files + n_state
    assert len(calls) == expected, (
        f"fadvise 调用数应恰好等于覆盖集大小（blob={n_blobs} + manifest={n_manifests} + "
        f"db/census/sessions={n_per_backup_files} + state={n_state} => {expected}）："
        f"实际={len(calls)}"
    )


# ---------------------------------------------------------------------------
# V15m：保留 cass-*/db 位腐——只有 ④ 自校验能抓，①blob 池 + ③链校验全过
# ---------------------------------------------------------------------------


@requires_cass
def test_v15m_retained_db_bitrot_caught_only_by_self_check(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v15m-night1")
    assert rc1 == 0, out1
    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v15m-night2")
    assert rc2 == 0, out2

    night1_db = dest / "cass-v15m-night1" / "db"
    original = night1_db.read_bytes()
    corrupted = bytearray(original)
    corrupted[0] ^= 0xFF
    night1_db.write_bytes(bytes(corrupted))

    # 反例断言：只做 ①blob 池全量扫描 + ③链校验——两者都不读 db 内容，全过。
    blobs_root = dest / "raw-mirror" / "v1" / "blobs"
    assert cass_weekly.verify_blob_pool(blobs_root) == [], (
        "反例应证伪：blob 池扫描不检查 db，db 不是内容寻址 blob"
    )
    assert cass_chain.verify_chain(dest, keep=7) == [], (
        "反例应证伪：链校验只比对 digest.json 之间的指针/哈希，不读 db 内容"
    )

    problems = cass_weekly.verify_weekly(dest, keep=7)
    assert any("db: FAILED" in p for p in problems), problems
    assert any("cass-v15m-night1" in p for p in problems), problems

    rc, out, err = _cli(dest, 7)
    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "db: FAILED" in out, out


# ---------------------------------------------------------------------------
# digest.json 损坏三态：缺字段 / 坏 JSON / 单文件 chmod 000（Task 14 review 留意项）
# ---------------------------------------------------------------------------


@requires_cass
def test_digest_missing_field_on_retained_backup_fails(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "digest-missing")
    assert rc == 0, out

    digest_path = dest / "cass-digest-missing" / "digest.json"
    digest = json.loads(digest_path.read_bytes())
    del digest["db_sha256"]
    digest_path.write_bytes(cass_common.dumps_canonical(digest))

    problems = cass_weekly.verify_weekly(dest, keep=7)
    assert any("db_sha256" in p for p in problems), problems


@requires_cass
def test_digest_bad_json_on_retained_backup_fails(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "digest-badjson")
    assert rc == 0, out

    digest_path = dest / "cass-digest-badjson" / "digest.json"
    digest_path.write_bytes(b"{not valid json")

    problems = cass_weekly.verify_weekly(dest, keep=7)
    assert problems, "坏 JSON 的 digest.json 必须让周校验 FAIL"


@requires_cass
def test_digest_chmod_000_on_retained_backup_fails_not_skip(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """Task 14 review 留意项：digest.json 单文件 `chmod 000` 的保留目录 → 周校验
    FAIL（不 skip）。链校验（③）已经会对同一场景给出 FAIL（`_scan_r` 的「digest
    内容层」语义），这里确认 ④ 自校验对同一目录独立捕获同一场景、周通道整体
    结论一致（都是 FAIL），不依赖只靠链校验兜底。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "digest-chmod000")
    assert rc == 0, out

    digest_path = dest / "cass-digest-chmod000" / "digest.json"
    digest_path.chmod(0o000)
    try:
        problems = cass_weekly.verify_weekly(dest, keep=7)
        backup_self_problems = cass_weekly.verify_backup_self(dest / "cass-digest-chmod000")
    finally:
        digest_path.chmod(0o644)

    assert problems, "digest.json 单文件权限损坏必须让周校验 FAIL，不是静默跳过"
    assert backup_self_problems, "④ 自校验独立遇到同一损坏也必须报问题，不依赖只靠链校验兜底"


@requires_cass
def test_digest_non_dict_scalar_on_retained_backup_fails(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """whole-branch review 修复项：`digest.json` 是合法 JSON 但裸标量（如 `5`）
    ——`read_digest` 原样返回它，后续 `"db_sha256" not in digest`/`field not in
    digest` 对 int 会 TypeError。必须干净 FAIL（不是 crash），语义与坏 JSON 同。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "digest-scalar")
    assert rc == 0, out

    digest_path = dest / "cass-digest-scalar" / "digest.json"
    digest_path.write_bytes(b"5")

    problems = cass_weekly.verify_weekly(dest, keep=7)
    assert problems, "非 dict digest.json（裸标量）必须让周校验 FAIL，不是 crash 或静默跳过"

    backup_self_problems = cass_weekly.verify_backup_self(dest / "cass-digest-scalar")
    assert backup_self_problems, "④ 自校验独立遇到同一损坏也必须干净报问题，不 TypeError"


# ---------------------------------------------------------------------------
# ⑤ sessions.state.tsv 首行篡改 → FAIL
# ---------------------------------------------------------------------------


@requires_cass
def test_state_header_tamper_fails(tmp_home, run_backup, synth_dd, cass_stub, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "state-tamper")
    assert rc == 0, out

    state_path = dest / "sessions.state.tsv"
    raw = state_path.read_bytes()
    _header, body = raw.split(b"\n", 1)
    tampered_header = b"#sha256 " + b"0" * 64
    state_path.write_bytes(tampered_header + b"\n" + body)

    problems = cass_weekly.verify_weekly(dest, keep=7)
    assert any("checksum mismatch" in p for p in problems), problems


# ---------------------------------------------------------------------------
# CLI：空 DEST（无任何已发布备份）→ exit 1
# ---------------------------------------------------------------------------


def test_cli_exit_1_on_empty_dest(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()

    rc, out, err = _cli(dest, 7)
    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "[weekly] FAIL" in out


# ---------------------------------------------------------------------------
# bash 层（backup-cass.sh step 18）：VERIFY_DOW 匹配/不匹配今天
# ---------------------------------------------------------------------------


def _today_dow() -> str:
    return subprocess.run(
        ["date", "+%u"], capture_output=True, text=True, timeout=5
    ).stdout.strip()


@requires_cass
def test_bash_layer_verify_dow_match_healthy_runs_weekly_stays_exit_0(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    today = _today_dow()
    other = str(int(today) % 7 + 1)

    # night1 显式错开今天，避免今天恰好是默认 VERIFY_DOW=7 时提前触发周校验。
    rc1, out1 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "bash-night1",
        extra_env={"CASS_BACKUP_VERIFY_DOW": other},
    )
    assert rc1 == 0, out1

    rc2, out2 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "bash-night2",
        extra_env={"CASS_BACKUP_VERIFY_DOW": today},
    )
    assert rc2 == 0, out2
    assert "weekly verify: running" in out2, out2
    assert "[weekly] PASS" in out2, out2


@requires_cass
def test_bash_layer_verify_dow_match_weekly_fail_forces_nonzero_exit(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    today = _today_dow()
    other = str(int(today) % 7 + 1)

    rc1, out1 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "bashfail-night1",
        extra_env={"CASS_BACKUP_VERIFY_DOW": other},
    )
    assert rc1 == 0, out1

    # 孤儿 blob（不被任何 manifest 引用）——注入后不影响 nightly 各门（Tier0/14b
    # 只检查被引用的 blob），只有周校验的全池扫描能抓到，藉此干净地演示「备份
    # 本身发布成功，但周校验 FAIL 必须让脚本整体 exit 非零」。
    nas_blobs_root = dest / "raw-mirror" / "v1" / "blobs"
    orphan_content = b"bash-layer-orphan-blob-corrupted-before-second-night"
    orphan_hash = blake3.blake3(orphan_content).hexdigest()
    orphan_path = nas_blobs_root / "blake3" / orphan_hash[:2] / f"{orphan_hash}.raw"
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    corrupted = bytearray(orphan_content)
    corrupted[0] ^= 0xFF
    orphan_path.write_bytes(bytes(corrupted))  # 文件名仍是原内容的 hash，内容已不符

    rc2, out2 = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "bashfail-night2",
        extra_env={"CASS_BACKUP_VERIFY_DOW": today},
    )

    assert rc2 != 0, out2
    assert "weekly verify: running" in out2, out2
    assert "[weekly] FAIL" in out2, out2
    assert str(orphan_path) in out2, out2
    assert "weekly deep verify (step 18) FAILED" in out2, out2
    # 备份本身仍应发布成功——周校验失败不回滚已发布的备份。
    assert (dest / "cass-bashfail-night2" / "COMPLETE").is_file(), out2


@requires_cass
def test_bash_layer_verify_dow_mismatch_skips_weekly(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    today = int(_today_dow())
    other = str(today % 7 + 1)

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "bash-skip",
        extra_env={"CASS_BACKUP_VERIFY_DOW": other},
    )
    assert rc == 0, out
    assert "weekly verify: skipped" in out, out
    assert "[weekly]" not in out, out
