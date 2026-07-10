"""CASS 备份 PR1 周深度校验 `cass_weekly.py`（spec §6.5，数据流 step 18）。

`backup-cass.sh` 每晚只验证「本轮写入的东西」（五腿门 + Tier 0 门 + O_DIRECT 读回 +
14a/14b 发布前闭合）——rsync 自身的传输后校验只覆盖「被传输过的文件」，
`--ignore-existing` 会让 NAS 上**早已存在**（哪怕早已损坏）的 blob 永远被跳过。
`VERIFY_DOW` 命中的那天，本模块把五件事补上（缺一不可，逐字对应 spec §6.5）：

  1. **blob 池全量 blake3 重算**：`$DEST/raw-mirror/v1/blobs/blake3/*/*.raw` 每个
     文件读前 fadvise(DONTNEED)，重算 BLAKE3 必须等于文件名（内容寻址存储的唯一
     判据）。**扫全池，不是只扫被引用的 blob**——manifest 随 keep-N 轮转掉，blob
     池永不删（无 `--delete`），只被已轮转备份引用过的 blob 会变成没有 manifest
     指向的孤儿，闭合检查（第 2 步）够不到它们。
  2. **恢复点闭合**：所有保留（含 `COMPLETE`）的 `cass-*/manifests/` 引用的
     `blob_blake3` 在池里存在（只 `stat`，不重算内容——内容由第 1 步的全池扫描
     兜底；这一步单独存在的价值是「被引用的 blob 缺失」这一更弱的错误也能报，
     且报文能指认是哪份 manifest 引用了它）。
  3. **sidecar 链校验**：复用 `cass_chain.verify_chain(dest, keep)`（spec §8.3
     算法逐字实现，PR2 restore 前置复用同一函数）。
  4. **每个保留 `cass-*/` 自校验**（rev18 才补齐的一条——前三步全过，`db` /
     `manifests.sha256sum` / `census.tsv` / `sessions.tsv` 若在 NAS 上腐烂，一条
     都发现不了，会一路静默留到 restore 那天才爆）：
       - `db` 的 sha256 == `digest.json.db_sha256` == 同目录 `db.sha256` 文件内容
       - `manifests.sha256sum` 逐文件核验（复用
         `cass_manifest_census.verify_manifests_sha256sum`，Fadvise 已在其内部）
       - `census.tsv` / `sessions.tsv` 的 sha256 == `digest.json` 对应字段
     **这一步读到的 NAS 文件（db / 每份 manifest / census.tsv / sessions.tsv）
     全部经 `cass_common.sha256_file(fadvise=True)` 或
     `cass_manifest_census.verify_manifests_sha256sum`（内部同样 fadvise）—— 不
     shell out 到 `sha256sum -c`（后者不带 fadvise，绕不过「刚写完立刻读，读到的
     是本地页缓存」这一类问题；本机页缓存会遮住 NAS 侧/跨客户端的腐烂，codex
     R3-P1）。** `db.sha256`/`manifests.sha256sum` 自身是几十字节的文本文件，只
     记录别人的哈希，不是被验证的内容寻址产物本身——用普通 `read_text` 读取其
     文本内容，不占 fadvise 覆盖集（覆盖集 = blob 池全部 + db + manifests + census.tsv
     + sessions.tsv + sessions.state.tsv，见 `verify_backup_self`/`verify_blob_pool`/
     `verify_state_header` 与测试 V14c）。
  5. **`$DEST/sessions.state.tsv`** 首行 `#sha256` 自校验（`cass_common.state_read`；
     `StateCorrupt` → FAIL）。**读前同样先 fadvise(DONTNEED)**——页缓存陈旧性与
     文件大小无关：⑤ 存在的全部理由就是「验证 NAS 上的字节」，若读到的是本机页
     缓存里每晚 13e 刚写完的副本，NAS 侧/跨客户端的腐烂被完全遮住，首行自校验
     形同虚设（review 修复：初版曾以「文件小 + 自带完整性头」豁免，理由不成立）。

`verify_weekly(dest, keep) -> list[str]`：空列表 = PASS，非空 = FAIL（每条一个具体
问题，人读得懂）。语义与 `cass_chain.verify_chain` 一致：**FAIL 不是 skip**——digest.json
缺失/坏 JSON/权限损坏（如某份保留目录的 `digest.json` 被单独 `chmod 000`）的保留
目录，本模块必须把它记成一条问题而不是悄悄跳过；`verify_chain` 已经对同一目录
给出 FAIL（见其 `_scan_r` 的「digest 内容层」语义），本模块的第 4 步对同一目录
独立重复这条判断，两者叠加不改变整体判定（仍是 FAIL），只是报文各自独立。

`infra/backup/cass/` 不是 package——同目录模块互相 import 的约定是在模块顶部
`sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` 后直接 import。

PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 偏好 / 基建拓扑 / 真实会话内容。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cass_chain  # noqa: E402 — 同目录 import 约定见模块 docstring
import cass_common  # noqa: E402
import cass_manifest_census  # noqa: E402


def _iter_retained(dest: pathlib.Path) -> list[pathlib.Path]:
    """保留集 R（spec §8.3 定义，与 `cass_chain._scan_r`/`cass_common._iter_published`
    同一目录探测层约定）：`dest` 下所有含 `COMPLETE` 的 `cass-*/` 目录。目录探测层
    （`is_dir`/`COMPLETE` 存在性）刻意不包 try——OS 级错误（如整个目录 `chmod 000`）
    照常上抛，与其余两个扫描器同一约定：DEST 权限坏是环境事件，必须响亮失败。"""
    return [
        entry
        for entry in sorted(dest.glob("cass-*"))
        if entry.is_dir() and (entry / "COMPLETE").exists()
    ]


def verify_blob_pool(blobs_root: pathlib.Path) -> list[str]:
    """① blob 池全量 blake3 重算 == 文件名（spec §6.5 第 2 条）。每个文件读前
    `posix_fadvise(DONTNEED)`（`cass_common.blake3_file(fadvise=True)`），否则刚
    同步过去的 blob 会从客户端缓存读回，校验的是本地内存而非 NAS 内容。

    扫全池（`blake3/*/*.raw`），不是只扫被引用的 blob——见模块 docstring 第 1 条
    的理由（manifest 随 keep-N 轮转、blob 池永不删）。`blobs_root` 不存在（全新
    DEST，尚无任何备份）时没有内容可扫，返回空列表，不是错误。
    """
    if not blobs_root.is_dir():
        return []
    problems: list[str] = []
    for blob_path in sorted(blobs_root.glob("blake3/*/*.raw")):
        expected = blob_path.name[:-len(".raw")]
        actual = cass_common.blake3_file(blob_path, fadvise=True)
        if actual != expected:
            problems.append(
                f"blob 池内容损坏: {blob_path}（重算 BLAKE3={actual}, 文件名={expected}）"
            )
    return problems


def verify_closure(retained_dirs: list[pathlib.Path], blobs_root: pathlib.Path) -> list[str]:
    """② 恢复点闭合检查（spec §6.5 第 3 条）：遍历每个保留 `cass-*/manifests/`，
    断言其引用的每个 `blob_blake3` 在池里存在（只 `stat`，内容由 ① 的全池扫描
    兜底——两步分工不同：① 不关心引用关系、只管「池里的东西内容对不对」；②
    不重算内容、只管「manifest 想要的东西还在不在」，报文能指认具体是哪份
    manifest 引用了缺失的 blob）。manifest 本身解析失败也记一条问题（防御性——
    正常情况下这类损坏已经被 ④ 的 `manifests.sha256sum` 校验单独抓到，这里
    重复报告不影响整体判定，仍是 FAIL）。"""
    problems: list[str] = []
    for backup_dir in retained_dirs:
        manifests_dir = backup_dir / "manifests"
        if not manifests_dir.is_dir():
            problems.append(f"{backup_dir.name}: manifests/ 目录缺失，闭合检查无法进行")
            continue
        for record in cass_manifest_census.parse_manifests(manifests_dir):
            if not record.ok:
                problems.append(
                    f"{backup_dir.name}/{record.path.name}: 无法解析（{record.error}），闭合检查无法核对该条引用"
                )
                continue
            pool_path = cass_manifest_census.blob_path_for(blobs_root, record.blob_blake3)
            if not pool_path.exists():
                problems.append(
                    f"{backup_dir.name}/{record.path.name}: 引用的 blob 不在池中: {pool_path}"
                )
    return problems


def _read_first_hash(path: pathlib.Path) -> str:
    """读 `sha256sum <file> > <out>` 格式的单行输出（如 `db.sha256`），取哈希段。
    普通 `read_text`——这几十字节的文本文件只是别人的哈希记录，不是被验证的内容
    寻址产物本身，不占 fadvise 覆盖集（见模块 docstring）。"""
    line = path.read_text(encoding="utf-8").strip()
    return line.split()[0] if line else ""


def verify_backup_self(backup_dir: pathlib.Path) -> list[str]:
    """④ 单个保留 `cass-*/` 的自校验（spec §6.5 第 5 条，rev18 才补齐）：
      - `db` 的 sha256 == `digest.json.db_sha256` == 同目录 `db.sha256` 文件内容
      - `manifests.sha256sum` 逐文件核验（`cass_manifest_census.verify_manifests_sha256sum`）
      - `census.tsv` / `sessions.tsv` 的 sha256 == `digest.json` 对应字段

    `digest.json` 读取失败（含权限损坏，如单文件 `chmod 000`）/ 坏 JSON / 缺失 ⇒
    记一条问题（FAIL，不是静默跳过，呼应 `cass_chain` 对同一场景的判定），但**不
    early-return**：db↔db.sha256 与 manifests↔manifests.sha256sum 两组比较不依赖
    digest.json，照常执行（取证完整性）；只有 digest 派生的三个字段比较随之跳过。"""
    problems: list[str] = []
    name = backup_dir.name

    # digest.json 读取失败（含单文件 chmod 000）/坏 JSON/缺失 ⇒ 记问题但**不
    # early-return**：db↔db.sha256 与 manifests↔manifests.sha256sum 两组比较不
    # 依赖 digest.json，照常执行（取证完整性——digest 坏的那份备份，db 本体是否
    # 也烂了是独立且更要紧的事实，review Minor 修复）。只有 digest 派生的比较
    # （db_sha256/census_sha256/sessions_tsv_sha256 三个字段）在 digest 不可读时
    # 跳过——那不是 skip 语义，digest 坏本身已经是一条 FAIL。
    digest = None
    try:
        digest = cass_common.read_digest(backup_dir)
        if digest is None:
            problems.append(f"{name}: 缺 digest.json")
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"{name}: digest.json 读取失败（{type(exc).__name__}: {exc}）")

    # --- db 三方一致：重算（fadvise）== digest.json.db_sha256 == db.sha256 文件内容 ---
    db_path = backup_dir / "db"
    db_sha_path = backup_dir / "db.sha256"
    if not db_path.is_file():
        problems.append(f"{name}: db 文件缺失")
    else:
        actual_db_sha256 = cass_common.sha256_file(db_path, fadvise=True)
        if digest is not None:
            if "db_sha256" not in digest:
                problems.append(f"{name}: digest.json 缺 db_sha256 字段")
            elif actual_db_sha256 != digest["db_sha256"]:
                problems.append(
                    f"{name}: db: FAILED（重算={actual_db_sha256}, digest.json.db_sha256={digest['db_sha256']}）"
                )
        if not db_sha_path.is_file():
            problems.append(f"{name}: db.sha256 文件缺失")
        else:
            recorded = _read_first_hash(db_sha_path)
            if recorded != actual_db_sha256:
                problems.append(
                    f"{name}: db: FAILED（db.sha256 记录={recorded}, 重算={actual_db_sha256}）"
                )

    # --- manifests 快照完整性：逐文件对 manifests.sha256sum（内部已 fadvise） ---
    manifests_dir = backup_dir / "manifests"
    sha256sum_path = backup_dir / "manifests.sha256sum"
    if not manifests_dir.is_dir():
        problems.append(f"{name}: manifests/ 目录缺失")
    elif not sha256sum_path.is_file():
        problems.append(f"{name}: manifests.sha256sum 缺失")
    else:
        ok, sub_problems = cass_manifest_census.verify_manifests_sha256sum(
            manifests_dir, sha256sum_path
        )
        if not ok:
            problems.extend(f"{name}: {p}" for p in sub_problems)

    # --- census.tsv / sessions.tsv 的 sha256 == digest.json 对应字段 ---
    if digest is not None:
        for field, filename in (
            ("census_sha256", "census.tsv"),
            ("sessions_tsv_sha256", "sessions.tsv"),
        ):
            path = backup_dir / filename
            if field not in digest:
                problems.append(f"{name}: digest.json 缺 {field} 字段")
                continue
            if not path.is_file():
                problems.append(f"{name}: {filename} 缺失")
                continue
            actual = cass_common.sha256_file(path, fadvise=True)
            if actual != digest[field]:
                problems.append(
                    f"{name}: {filename}: FAILED（重算={actual}, digest.json.{field}={digest[field]}）"
                )

    return problems


def verify_state_header(state_path: pathlib.Path) -> list[str]:
    """⑤ `$DEST/sessions.state.tsv` 首行 `#sha256` 自校验（spec §6.5 第 6 条）。
    缺失该文件视为 FAIL（它是共享权威状态，spec §6.3.1：state 消失是完整性事件）。

    读前先 fadvise(DONTNEED)——页缓存陈旧性与文件大小无关（见模块 docstring 第
    5 条）。`cass_common.state_read` 不带 fadvise 参数（其余调用方都是读本机刚写
    的文件，语义不同），这里用一个短暂 fd 对同一 inode 丢页缓存（fadvise 作用于
    页缓存，跨 fd 生效），随后 `state_read` 的独立 open 即回源读，无需改共享签名。
    """
    if not state_path.is_file():
        return [f"{state_path.name}: sessions.state.tsv 缺失"]
    fd = os.open(state_path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)
    try:
        cass_common.state_read(state_path)
    except cass_common.StateCorrupt as exc:
        return [str(exc)]
    return []


def verify_weekly(dest, keep: int) -> list[str]:
    """spec §6.5 五件事的总入口（sessions 通道由每晚发布门覆盖，周通道不重复，
    见模块 docstring）。空列表 = PASS，非空 = FAIL。"""
    dest = pathlib.Path(dest)
    blobs_root = dest / "raw-mirror" / "v1" / "blobs"
    problems: list[str] = []

    problems.extend(verify_blob_pool(blobs_root))

    retained = _iter_retained(dest)
    problems.extend(verify_closure(retained, blobs_root))

    problems.extend(cass_chain.verify_chain(dest, keep))

    for backup_dir in retained:
        problems.extend(verify_backup_self(backup_dir))

    problems.extend(verify_state_header(dest / "sessions.state.tsv"))

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cass_weekly.py")
    parser.add_argument("--dest", required=True, help="已发布备份的 DEST 根目录")
    parser.add_argument("--keep", required=True, type=int, help="keep-N 轮转的 N（供链校验用）")
    args = parser.parse_args(argv)

    problems = verify_weekly(args.dest, args.keep)
    if not problems:
        print("[weekly] PASS")
        return 0

    print("[weekly] FAIL:")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
