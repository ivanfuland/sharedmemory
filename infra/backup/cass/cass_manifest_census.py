"""CASS 备份 PR1 Tier 0 门 —— 独立 manifest 普查，交叉验证 `cass doctor --json`（spec §5.6）。

`raw_mirror.summary.*` 是 doctor 自己算出来的数——「零错误」与「根本没检查」在计数器上
长得一模一样（受限环境实测 doctor 1.14 秒就返回并报 `status=verified`，不可能验完
GB 级 blob）。本模块独立重新解析锁内 manifest 快照（不信任 doctor 的自述），与 doctor
的计数器做交叉恒等；任一不符、或普查自身解析失败，一律 FAIL——绝不静默跳过。

调用位置 = 写锁内（§5.6「源端，锁内」）：普查与 doctor 必须看同一个锁内状态，由
`backup-cass.sh`（Task 9）负责在正确时机调本 CLI；本文件只是判定逻辑本体。

Task 10 新增 `--publish-check` 子命令（spec §6 step 14b 发布前闭合检查）：复用
`parse_manifests`/`blob_path_for`，独立于 Tier 0 普查——只由 `backup-cass.sh` 在
`.incomplete-$STAMP/manifests/` 落盘之后、发布前调用，校验每个 manifest 的
`blob_relative_path` 形状/一致性 + NAS blob 池的存在性/`st_size`/内容 BLAKE3。

`infra/backup/cass/` 不是 package——同目录模块互相 import 的约定是在模块顶部
`sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` 后直接 import。

PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 偏好 / 基建拓扑 / 真实会话内容。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cass_common  # noqa: E402 — 同目录 import 约定见模块 docstring

_BLAKE3_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# spec §5.6 判据 2：这五个必须全为 0。
_ZERO_COUNTERS: tuple[str, ...] = (
    "missing_blob_count",
    "checksum_mismatch_count",
    "manifest_checksum_mismatch_count",
    "invalid_manifest_count",
    "interrupted_capture_count",
)

# spec §5.6 判据 3：交叉恒等用到的三个 doctor 计数器。
_CROSS_CHECK_COUNTERS: tuple[str, ...] = (
    "manifest_count",
    "verified_blob_count",
    "duplicate_blob_reference_count",
)

# doctor JSON 的 raw_mirror.summary 必须齐全这八个键（且均为 int）才可信——
# 缺键 / 非 int 一律当不可信输入处理（喂进来的是外部数据，缺键 = 没检查 = FAIL，
# 参考 spec §5.6 V5h「零错误与没检查同形」的精神）。
_REQUIRED_SUMMARY_KEYS: tuple[str, ...] = _ZERO_COUNTERS + _CROSS_CHECK_COUNTERS


@dataclass
class ManifestRecord:
    """单个 manifest 文件的解析结果。`ok=False` 时 `blob_blake3` 为 None——
    该文件计入 `unparseable`，不贡献任何 blob 引用（Task 10 的 `--publish-check`
    会复用本记录取 `raw` 里的 `blob_relative_path`/`blob_size_bytes` 等字段）。"""

    path: pathlib.Path
    ok: bool
    blob_blake3: str | None
    raw: dict | None
    error: str | None


def parse_manifests(manifests_dir: pathlib.Path) -> list[ManifestRecord]:
    """解析 `manifests_dir` 下每个 `*.json`。

    JSON 解析失败 / 顶层不是对象 / 缺 `blob_blake3` 键 / 该键不匹配 `^[0-9a-f]{64}$`
    —— 均记为该文件的失败（`ok=False`），**绝不静默跳过**：spec §5.6 明确指出
    「解析失败 ⇒ 该 manifest 贡献 0 个引用」会让三条交叉恒等式恰好凑上，是标准的
    假绿路径。调用方必须把失败计入 `unparseable` 并强制 FAIL。
    """
    records: list[ManifestRecord] = []
    for path in sorted(manifests_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            records.append(ManifestRecord(path, False, None, None, f"JSON 解析失败: {exc}"))
            continue

        blob_blake3 = raw.get("blob_blake3") if isinstance(raw, dict) else None
        # `.fullmatch()` 而非 `.match()`：Python 的 `$` 会匹配到 trailing newline
        # **之前**，`.match()` + `^...$` 对「64 hex + 尾随 \n」过匹配——65 字节的
        # 坏 hash 会溜过 unparseable 桶，错位报成下游的「blob 文件缺失」且 stdout
        # 的 `unparseable=0` 误导取证（review 修复）。
        if not isinstance(blob_blake3, str) or not _BLAKE3_HEX_RE.fullmatch(blob_blake3):
            records.append(
                ManifestRecord(
                    path, False, None, raw if isinstance(raw, dict) else None,
                    f"blob_blake3 缺失或不匹配 ^[0-9a-f]{{64}}$: {blob_blake3!r}",
                )
            )
            continue

        records.append(ManifestRecord(path, True, blob_blake3, raw, None))
    return records


@dataclass
class CensusResult:
    """独立普查三元组 + `unparseable`（spec §5.6 判据 3）。

    `manifest_count`：目录下全部 `*.json` 文件数，不论解析成败——与 doctor 自身把
    `manifest_count` 和 `invalid_manifest_count` 分列的结构同构，让「文件总数」这
    一条交叉检查独立于「内容能否解析」。
    `unique_blobs`/`duplicate_refs`：只统计可解析记录。每个 manifest 恰好引用 1
    个 blob ⇒ `unique_blobs + duplicate_refs == 可解析 manifest 数`，在
    `unparseable == 0` 时即 `== manifest_count`（结构必然，spec §5.6 附注）。
    """

    manifest_count: int
    unique_blobs: int
    duplicate_refs: int
    unparseable: int
    unparseable_detail: list[str]


def census_manifests(manifests_dir: pathlib.Path) -> tuple[CensusResult, list[ManifestRecord]]:
    records = parse_manifests(manifests_dir)
    ok_records = [r for r in records if r.ok]
    bad_records = [r for r in records if not r.ok]

    seen: set[str] = {r.blob_blake3 for r in ok_records}
    unique_blobs = len(seen)
    duplicate_refs = len(ok_records) - unique_blobs

    result = CensusResult(
        manifest_count=len(records),
        unique_blobs=unique_blobs,
        duplicate_refs=duplicate_refs,
        unparseable=len(bad_records),
        unparseable_detail=[f"{r.path.name}（{r.error}）" for r in bad_records],
    )
    return result, records


def blob_path_for(blobs_root: pathlib.Path, blob_blake3: str) -> pathlib.Path:
    """spec §5.6：blob 磁盘路径**只由** `blob_blake3` 推导
    （`<blobs-root>/blake3/<前2位>/<64位>.raw`），**绝不取 manifest 的任何路径字段**
    ——无路径穿越面。调用方必须已用 `_BLAKE3_HEX_RE` 校验过 `blob_blake3`。
    """
    return blobs_root / "blake3" / blob_blake3[:2] / f"{blob_blake3}.raw"


def find_missing_blobs(records: list[ManifestRecord], blobs_root: pathlib.Path) -> list[str]:
    """对普查引用到的每个 blob（按 blake3 去重）逐个 `stat`（不读内容），返回缺失
    的磁盘路径列表（人读得懂，供 stdout 指认）。"""
    missing: list[str] = []
    seen: set[str] = set()
    for r in records:
        if not r.ok or r.blob_blake3 in seen:
            continue
        seen.add(r.blob_blake3)
        path = blob_path_for(blobs_root, r.blob_blake3)
        if not path.exists():
            missing.append(str(path))
    return missing


# spec §6 step 14b 形状校验：`blobs/blake3/<前2位>/<64位hex>.raw`，捕获两个分组供
# basename/目录一致性检查复用（避免重复手切片字符串）。
_PUBLISH_BLOB_PATH_RE = re.compile(
    r"^blobs/blake3/(?P<dir2>[0-9a-f]{2})/(?P<hash>[0-9a-f]{64})\.raw$"
)


def run_publish_check(manifests_dir: pathlib.Path, blobs_root: pathlib.Path) -> tuple[bool, list[str]]:
    """spec §6 step 14b —— 发布前闭合检查（`--publish-check` 子命令本体）。

    对 `manifests_dir` 下每份 manifest：
      1. 无法解析（`parse_manifests` 判 `ok=False`）⇒ 直接记为问题——发布前不允许
         带着解析失败的 manifest 过关。
      2. `blob_relative_path` 必须精确匹配 `^blobs/blake3/[0-9a-f]{2}/[0-9a-f]{64}\\.raw$`
         （**不匹配 ⇒ 该字段到此为止，绝不用于任何文件系统操作**——manifest 来自被
         备份的数据，是不可信输入，V13d 的路径穿越构造靠这一步挡）。
      3. 匹配到的 basename（64 位 hex 分组）必须等于同一 manifest 的 `blob_blake3`，
         且目录前两位分组必须等于 `blob_blake3` 的前两位。
      4. **NAS blob 池里的路径永远由 `blob_path_for(blobs_root, blob_blake3)` 推导**
         （不是 `blob_relative_path`）：文件必须存在，`st_size == blob_size_bytes`
         （快速预检，未过不读内容），且读前 fadvise(DONTNEED) 后重算 BLAKE3 必须等于
         `blob_blake3`（内容寻址存储唯一判据）。

    返回 `(ok, problems)`；`problems` 为空即 `ok=True`。
    """
    records = parse_manifests(manifests_dir)
    problems: list[str] = []

    for record in records:
        if not record.ok:
            problems.append(f"{record.path.name}: 无法解析（{record.error}）")
            continue

        blob_blake3 = record.blob_blake3
        raw = record.raw or {}
        rel_path = raw.get("blob_relative_path")
        size_bytes = raw.get("blob_size_bytes")

        match = _PUBLISH_BLOB_PATH_RE.fullmatch(rel_path) if isinstance(rel_path, str) else None
        if match is None:
            problems.append(f"{record.path.name}: blob_relative_path 形状不符: {rel_path!r}")
            continue
        if match.group("hash") != blob_blake3:
            problems.append(
                f"{record.path.name}: blob_relative_path basename != blob_blake3 "
                f"({match.group('hash')} != {blob_blake3})"
            )
            continue
        if match.group("dir2") != blob_blake3[:2]:
            problems.append(
                f"{record.path.name}: blob_relative_path 目录前两位不符 "
                f"({match.group('dir2')} != {blob_blake3[:2]})"
            )
            continue

        # 路径只由 blob_blake3 推导——blob_relative_path 到此为止只做过形状/一致性
        # 校验，绝不参与文件系统操作（V13d：即便它被改成 `../../etc/passwd`，
        # 上面的形状校验已经先 FAIL，本行永远拿不到那个值）。
        pool_path = blob_path_for(blobs_root, blob_blake3)
        if not pool_path.is_file():
            problems.append(f"{record.path.name}: blob 池文件缺失: {pool_path}")
            continue

        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
            problems.append(f"{record.path.name}: blob_size_bytes 缺失或非 int: {size_bytes!r}")
            continue
        actual_size = pool_path.stat().st_size
        if actual_size != size_bytes:
            problems.append(
                f"{record.path.name}: st_size 不符（池={actual_size}, manifest={size_bytes}）"
            )
            continue  # 快速预检未过 ⇒ 不读内容重算 BLAKE3（st_size 不能单独当发布门，
            # 但已经不符时重算内容 hash 不会改变结论，省一次读）。

        actual_hash = cass_common.blake3_file(pool_path, fadvise=True)
        if actual_hash != blob_blake3:
            problems.append(
                f"{record.path.name}: blob 内容 BLAKE3 不符（池文件 {pool_path.name} 重算="
                f"{actual_hash}, 期望={blob_blake3}）"
            )

    return not problems, problems


def verify_manifests_sha256sum(
    manifests_dir: pathlib.Path, sha256sum_path: pathlib.Path
) -> tuple[bool, list[str]]:
    """spec §6 step 14a —— manifest 快照完整性门：核对 `manifests_dir` 下每个文件与
    `sha256sum_path`（`sha256sum manifests/*.json > out` 生成的格式，相对路径，两个
    空格分隔）记录的哈希，读前 `posix_fadvise(DONTNEED)`（`cass_common.sha256_file`
    的 `fadvise=True`）。裸 `sha256sum -c` 不带 fadvise，绕不过「刚写完立刻读，读到
    的是本地页缓存」这一类问题——同 §6.4 db 读回的机理，只是这里换用 fadvise 而非
    `O_DIRECT`（spec §6.4 开篇注：两处「绕过缓存」的手段有意不同）。

    `sha256sum_path` 内的相对路径以 `manifests_dir` 的**父目录**为基准（生成时
    `cd "$STG" && sha256sum manifests/*.json > manifests.sha256sum`，落 NAS 后
    `.incomplete-$STAMP/` 同构：`manifests_dir` = `.incomplete-$STAMP/manifests`，
    父目录正是 `.incomplete-$STAMP`）。

    **不能只靠 14b**（`run_publish_check`）：把某个 manifest 的内容整体换成另一份
    真实自洽的 manifest，14b 的四项检查全过（它校验的是替换后那份 manifest 自己
    引用的 blob）——只有这里的 `manifests.sha256sum` 比对能抓出「内容被整体调包」。

    返回 `(ok, problems)`；`problems` 为空即 `ok=True`。
    """
    base_dir = manifests_dir.parent
    problems: list[str] = []

    lines = sha256sum_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return False, ["manifests.sha256sum 为空"]

    for line in lines:
        expected_hash, sep, rel_path = line.partition("  ")
        if not sep or not expected_hash or not rel_path:
            problems.append(f"manifests.sha256sum 行格式不对: {line!r}")
            continue
        manifest_path = base_dir / rel_path
        if not manifest_path.is_file():
            problems.append(f"manifests.sha256sum 记录的文件缺失: {rel_path}")
            continue
        actual_hash = cass_common.sha256_file(manifest_path, fadvise=True)
        if actual_hash != expected_hash:
            problems.append(
                f"checksum mismatch: {rel_path}（期望={expected_hash}, 实际={actual_hash}）"
            )

    return not problems, problems


def load_doctor_summary(
    doctor_json_path: pathlib.Path,
) -> tuple[str | None, dict[str, int] | None, str | None]:
    """读取 doctor JSON，返回 `(status, summary, error)`。

    `error` 非 None 时前两者为 None——doctor JSON 自身读取/解析失败、缺
    `raw_mirror`/`status`/`summary`、或 `summary` 缺任一必需计数器键 / 某键非
    `int`（`bool` 是 `int` 子类，显式排除），一律视为不可信输入，直接 FAIL。
    不做「缺键当 0」之类的宽容解析——那等于「没检查」（spec §5.6 V5h 精神）。
    """
    try:
        raw = json.loads(doctor_json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, None, f"doctor JSON 读取/解析失败: {exc}"

    if not isinstance(raw, dict) or "raw_mirror" not in raw:
        return None, None, "doctor JSON 缺 'raw_mirror' 键"
    raw_mirror = raw["raw_mirror"]
    if not isinstance(raw_mirror, dict):
        return None, None, "doctor JSON 的 'raw_mirror' 不是对象"
    if "status" not in raw_mirror or "summary" not in raw_mirror:
        return None, None, "doctor JSON 的 raw_mirror 缺 'status' 或 'summary'"

    status = raw_mirror["status"]
    summary = raw_mirror["summary"]
    if not isinstance(summary, dict):
        return None, None, "doctor JSON 的 raw_mirror.summary 不是对象"

    bad_keys = [
        key
        for key in _REQUIRED_SUMMARY_KEYS
        if not isinstance(summary.get(key), int) or isinstance(summary.get(key), bool)
    ]
    if bad_keys:
        return None, None, (
            "doctor JSON 的 raw_mirror.summary 缺键或值非 int: " + ", ".join(bad_keys)
        )

    return status, {key: summary[key] for key in _REQUIRED_SUMMARY_KEYS}, None


def main(argv: list[str] | None = None) -> int:
    """Tier 0 门 CLI：spec §5.6 全部四条判据都过才 exit 0，否则 exit 1。

    四条判据（全部满足才 PASS）：
      1. `raw_mirror.status == "verified"`
      2. `raw_mirror.summary` 五计数器全 0
      3. 独立普查与 doctor 的三元组交叉恒等 + 普查自身 `unparseable==0 且
         manifest_count>0`
      4. 普查引用到的每个 blob 在 `--blobs-root` 下确实存在（stat，不读内容）

    判据 3/4 与判据 1/2 相互独立：doctor JSON 本身不可信（缺键/解析失败）时，
    判据 3 的「普查自身 fail-loud」与判据 4 的 blob 存在性检查仍照跑——不因为
    doctor 不可信就跳过本该独立验证的部分。

    `--publish-check`：切换到 spec §6 step 14b 发布前闭合检查（见
    `run_publish_check`），此时 `--doctor-json` 不需要；`--manifests-dir`/
    `--blobs-root` 复用同一对参数，语义变为「`.incomplete-$STAMP/manifests/`」
    与「`$DEST` 的共享 blob 池」。
    """
    parser = argparse.ArgumentParser(prog="cass_manifest_census.py")
    parser.add_argument("--manifests-dir", required=True, dest="manifests_dir")
    parser.add_argument("--doctor-json", dest="doctor_json", default=None)
    parser.add_argument("--blobs-root", required=True, dest="blobs_root")
    parser.add_argument(
        "--publish-check", action="store_true", dest="publish_check",
        help="spec §6 step 14b 发布前闭合检查，取代 Tier 0 普查+doctor 交叉验证模式",
    )
    args = parser.parse_args(argv)

    manifests_dir = pathlib.Path(args.manifests_dir)
    blobs_root = pathlib.Path(args.blobs_root)

    if args.publish_check:
        ok, problems = run_publish_check(manifests_dir, blobs_root)
        if problems:
            print("[FAIL] 发布前闭合检查未通过:")
            for p in problems:
                print(f"  - {p}")
        else:
            print("[PASS] 发布前闭合检查通过")
        return 0 if ok else 1

    if not args.doctor_json:
        parser.error("--doctor-json is required unless --publish-check is given")
    doctor_json_path = pathlib.Path(args.doctor_json)

    problems: list[str] = []

    census, records = census_manifests(manifests_dir)

    # 普查自身的 fail-loud 判据（判据 3 的后半句）——不依赖 doctor 是否可信。
    if census.unparseable != 0:
        problems.append(
            f"census.unparseable={census.unparseable}（须为 0）: "
            + "; ".join(census.unparseable_detail)
        )
    if census.manifest_count == 0:
        problems.append("census.manifest_count == 0（manifests-dir 为空或无 *.json 文件）")

    status, summary, doctor_error = load_doctor_summary(doctor_json_path)
    if doctor_error is not None:
        problems.append(f"doctor JSON 不可信: {doctor_error}")
    else:
        if status != "verified":
            problems.append(f'doctor.raw_mirror.status={status!r}（须为 "verified"）')
        for key in _ZERO_COUNTERS:
            if summary[key] != 0:
                problems.append(f"doctor.summary.{key}={summary[key]}（须为 0）")
        if census.manifest_count != summary["manifest_count"]:
            problems.append(
                f"恒等式不符: census.manifest_count={census.manifest_count} != "
                f"doctor.manifest_count={summary['manifest_count']}"
            )
        if census.unique_blobs != summary["verified_blob_count"]:
            problems.append(
                f"恒等式不符: census.unique_blobs={census.unique_blobs} != "
                f"doctor.verified_blob_count={summary['verified_blob_count']}"
            )
        if census.duplicate_refs != summary["duplicate_blob_reference_count"]:
            problems.append(
                f"恒等式不符: census.duplicate_refs={census.duplicate_refs} != "
                f"doctor.duplicate_blob_reference_count={summary['duplicate_blob_reference_count']}"
            )

    missing_blobs = find_missing_blobs(records, blobs_root)
    problems.extend(f"blob 文件缺失: {p}" for p in missing_blobs)

    print(
        f"[census] manifest_count={census.manifest_count} unique_blobs={census.unique_blobs} "
        f"duplicate_refs={census.duplicate_refs} unparseable={census.unparseable}"
    )
    if summary is not None:
        print(
            f"[doctor]  status={status!r} manifest_count={summary['manifest_count']} "
            f"verified_blob_count={summary['verified_blob_count']} "
            f"duplicate_blob_reference_count={summary['duplicate_blob_reference_count']} "
            + " ".join(f"{key}={summary[key]}" for key in _ZERO_COUNTERS)
        )
    else:
        print(f"[doctor]  不可信/不可解析（{doctor_error}）")

    if problems:
        print("[FAIL] Tier 0 门未通过:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("[PASS] Tier 0 门通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
