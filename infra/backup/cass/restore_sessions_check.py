#!/usr/bin/env python3
"""restore-cass.sh **会话源恢复 fail-closed 门**（spec §4.3 会话通道 + codex 2026-07-12 R10-[critical]）。

会话源恢复（`--sessions-into[-source]`）此前只从共享池 `$DEST/sessions/<alias>/` rsync，**不校验所选
备份**：NAS 池缺失 / 少 alias / 某 jsonl 腐烂时，DB+索引的 doctor/search 仍 PASS → 脚本**谎报成功**；
`--sessions-into-source --yes` 的源全丢场景下，生产会话源会保持空缺或不完整，脚本却返回 0。

判据（复制**之前**跑，缺一即 fail-closed）：
  - `sha256(sessions.tsv) == digest.sessions_tsv_sha256`（所选备份的 sessions 清单自洽；probe 实测：
    digest 字段 = **整个 sessions.tsv 文件**的 sha256，含 `#sha256` 首行）；
  - `sessions.tsv` 每条数据行（`<relpath>\t<size>\t<blake3>\t<status>`，跳过 `#`/空行）对应的
    `<pool_root>/<relpath>` **存在 + size 相符 + blake3 相符**（probe 实测：tsv 的 blake3 = pool 文件
    内容的 blake3；relpath 已含 alias 前缀，故 pool 路径 = `<pool_root>/<relpath>`）。

清单是「NAS 上有什么」的累积快照（spec §6.3.1）：`present` 与 `absent_at_source` 两种状态的文件**都在
NAS 上**，故都要在池内校验（`absent_at_source` = 源端已删、NAS 仍留，恢复要带回）。

用法：`restore_sessions_check.py <sessions.tsv> <digest.sessions_tsv_sha256 值> <pool_root>`。需 blake3
（restore 硬依赖，preflight 已校验）。
"""
from __future__ import annotations

import hashlib
import pathlib
import sys

import blake3


def check(sessions_tsv: str, digest_sha256: str, pool_root: str) -> str:
    tsv = pathlib.Path(sessions_tsv)
    pool = pathlib.Path(pool_root)
    if not tsv.is_file():
        raise SystemExit(f"[restore] FATAL: 缺 sessions.tsv（会话恢复被请求但备份无会话清单）: {tsv}")

    # 1) sessions.tsv 与 digest 锚点自洽（整文件 sha256）
    got = hashlib.sha256(tsv.read_bytes()).hexdigest()
    want = (digest_sha256 or "").strip()
    if not want or got != want:
        raise SystemExit(
            f"[restore] FATAL: sessions.tsv 与 digest.sessions_tsv_sha256 不符（会话清单不自洽/被篡改）"
            f" 期望 {want or '<空>'} 实算 {got}"
        )

    # 2) 逐行校验池内文件 存在 + size + blake3
    checked = 0
    for raw in tsv.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            raise SystemExit(f"[restore] FATAL: sessions.tsv 行格式坏（需 4 列 tab 分隔）: {raw!r}")
        rel, size_s, b3_want, _status = parts[0], parts[1], parts[2], parts[3]
        pf = pool / rel
        if not pf.is_file() or pf.is_symlink():
            raise SystemExit(f"[restore] FATAL: 池内缺文件/非普通文件（NAS 会话池不完整）: {rel}")
        try:
            want_size = int(size_s)
        except ValueError:
            raise SystemExit(f"[restore] FATAL: sessions.tsv size 列非整数: {raw!r}")
        actual_size = pf.stat().st_size
        if actual_size != want_size:
            raise SystemExit(
                f"[restore] FATAL: 池内文件 size 不符（截断/损坏）: {rel} 期望 {want_size} 实得 {actual_size}"
            )
        got_b3 = blake3.blake3(pf.read_bytes()).hexdigest()
        if got_b3 != b3_want:
            raise SystemExit(
                f"[restore] FATAL: 池内文件 blake3 不符（腐烂）: {rel} 期望 {b3_want} 实算 {got_b3}"
            )
        checked += 1

    if checked == 0:
        raise SystemExit("[restore] FATAL: sessions.tsv 无数据行（会话清单为空，拒绝静默无恢复）")
    return f"[restore] 会话门 OK：{checked} 条会话在池内校验通过（sha256 自洽 + 存在/size/blake3 全符）"


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("用法: restore_sessions_check.py <sessions.tsv> <digest.sessions_tsv_sha256> <pool_root>")
    print(check(sys.argv[1], sys.argv[2], sys.argv[3]))
