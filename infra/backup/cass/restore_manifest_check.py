#!/usr/bin/env python3
"""restore-cass.sh step 2 的 **manifest 精确快照门**（spec §4.3 step 4：restore 出的 raw-mirror
必须是所选备份的精确快照）。

判据（缺一即 fail-closed）：
  - `manifests.sha256sum` 解析出的 relpath 集合 == `manifests/` 目录里实际 `*.json` 的 relpath 集合；
  - sidecar **无重复** relpath；
  - `manifests/` 里**无 symlink / 非普通文件**（`find -type f` 会漏 symlink，但 `cp -a` 会照复制、
    仓内 reader 用 `Path.glob('*.json')` 会读到 → 非精确快照混入）。

只比数量不够（codex 2026-07-12 R6-[critical]）：symlink 逃过 `find -type f`、重复 sidecar 行 +
漏列另一个，都是"数量相等但集合不等"。故这里做**逐项集合比较** + symlink 拒绝 + 去重。

用法：`restore_manifest_check.py <manifests.sha256sum 路径> <manifests 目录>`。纯 stdlib。
"""
from __future__ import annotations

import pathlib
import sys


def check(sidecar_path: str, manifests_dir: str) -> str:
    sidecar = pathlib.Path(sidecar_path)
    mdir = pathlib.Path(manifests_dir)
    if not sidecar.is_file():
        raise SystemExit(f"[restore] FATAL: 缺 manifests.sha256sum: {sidecar}")
    if not mdir.is_dir():
        raise SystemExit(f"[restore] FATAL: 缺 manifests 目录: {mdir}")

    # 解析 sidecar：每行 `<64hex>␣␣<relpath>`（二进制模式 `<64hex>␣*<relpath>`）
    listed: list[str] = []
    for raw in sidecar.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise SystemExit(f"[restore] FATAL: manifests.sha256sum 行格式坏: {raw!r}")
        rel = parts[1].lstrip("*")  # 去二进制模式前缀
        listed.append(rel)

    if not listed:
        raise SystemExit("[restore] FATAL: manifests.sha256sum 为空")

    # 拒重复 relpath（数量相等的伪装之一）
    dups = sorted({r for r in listed if listed.count(r) > 1})
    if dups:
        raise SystemExit(f"[restore] FATAL: manifests.sha256sum 有重复路径: {dups[:5]}")
    listed_set = set(listed)

    # 目录实际 *.json：用 glob（含 symlink），逐个拒 symlink / 非普通文件
    actual_set: set[str] = set()
    for p in sorted(mdir.glob("*.json")):
        if p.is_symlink() or not p.is_file():
            raise SystemExit(f"[restore] FATAL: manifests/ 含 symlink/非普通文件（非精确快照）: {p.name}")
        actual_set.add(f"manifests/{p.name}")

    if listed_set != actual_set:
        extra = sorted(actual_set - listed_set)
        missing = sorted(listed_set - actual_set)
        raise SystemExit(
            f"[restore] FATAL: manifests 集合与 sidecar 不符（非精确快照）"
            f" extra={extra[:5]} missing={missing[:5]}"
        )
    return f"[restore] step 2 OK：{len(actual_set)} 个 manifest 集合恒等（无多余/缺失/重复/symlink）"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("用法: restore_manifest_check.py <manifests.sha256sum> <manifests 目录>")
    print(check(sys.argv[1], sys.argv[2]))
