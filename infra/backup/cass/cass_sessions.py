"""CASS 备份 PR1 sessions 通道 A —— 源端前缀校验 / itemize 解析（spec §6.3.1，
数据流 step 13b-13d）。

两个子命令：

`check-source`：只读源端（`--roots` 指向的目录）+ 共享权威状态
`$DEST/sessions.state.tsv`（由调用方 `--state` 传入，本模块不知道也不关心 DEST 长
什么样——**函数签名/CLI 参数不含任何 NAS/DEST 路径**，只读源端 + 一份状态文件）。
对状态里标 `present` 且此刻仍存在于源端的每条记录，核对
`size(src) >= nas_size` 与 `blake3(src[0:nas_size]) == 记录 hash`；任一不成立 ⇒
该文件判为「异常」，排除出本次同步。`absent_at_source` 记录直接跳过（源端本来就
没有，无从比对）；此刻源端也没有该文件的 `present` 记录同样跳过（比对不了，交给
Task 12 的 13e/13f 全量回读门收口）。

`--quarantine`/`--quarantine-reason`（与 spec §5.7 rebaseline 同构的人工放行通
道）点名的文件无条件排除出同步，且**不计入异常**——它的存在意义正是让操作者显式
承认「这个文件我知道它坏、也知道为什么，别再天天因为它而不发布」，语义上是给
腿门开一个人工签字的旁路，同 rebaseline 对五腿门「不是 bypass-all，只关掉与历史
基线的比对」的定位一致：quarantine 只关掉这一个文件的「异常即挡发布」，其余判据
（其它文件的前缀校验、itemize 解析…）照跑不误。

产出：**每个 root 一个** exclude 文件 `<out-exclude-dir>/exclude.<alias>`（root
相对路径、每行前导 `/` 锚定——不能用一份全局 exclude，因为 state 记的是
`alias/子路径`，rsync 是按各 root 相对匹配，全局共用会 alias 串味 + 跨 root 同名
碰撞）。即使某个 root 一个异常都没有，exclude 文件也必须生成（哪怕是空的）——
`rsync --exclude-from` 吃空文件是安全的，调用方不必再判「有没有异常」才决定要不
要传这个参数。

exit code：0 = 全净；3 = 有非隔离的异常文件（healthy 部分仍照常同步，但整次备份
最终不发布——由调用方 `backup-cass.sh` 落地）；1 = 内部错误（参数非法、quarantine
缺 reason、state/roots 引用不一致的 alias、state 文件本身损坏等）——内部错误路径
不落任何 exclude 文件，调用方应直接判为硬失败。

`parse-itemize`：解析一份 `rsync -ai` 的 itemize 输出（`YXcstpoguax path` 格式，
11 位标志 + 一个空格 + 路径），分流规则见 spec step 13d：

    ^>f   ⇒ 该文件被传输了内容 → 记入，输出其路径字段
    ^cd   ⇒ 创建目录            → 忽略
    ^\\.d ⇒ 目录属性变更        → 忽略
    ^\\.f ⇒ 文件仅属性变更      → 忽略
    空行  ⇒ 忽略
    其余任何行 ⇒ exit 1，不产出任何 stdout（fail-closed——未知行形态是「解析器没
    见过的输出」，宁可让调用方走 fail_incomplete，也不能假装看懂了）

`infra/backup/cass/` 不是 package——同目录模块互相 import 的约定是在模块顶部
`sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` 后直接 import。

PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 偏好 / 基建拓扑 / 真实会话内容。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cass_common  # noqa: E402 — 同目录 import 约定见模块 docstring

_PREFIX = "[cass_sessions]"


def _fatal(msg: str) -> None:
    print(f"{_PREFIX} FATAL: {msg}", file=sys.stderr)


def _parse_roots(spec: str) -> dict[str, pathlib.Path] | None:
    """`alias=path:alias=path...`——冒号分隔对、等号分隔键值。**约定：路径本身不
    得含冒号**（无转义机制，含冒号的路径会被错误切分；这是本 CLI 与 bash 调用方
    共享的已知边界，不在本 task 范围内解决）。格式非法 ⇒ 返回 None。"""
    roots: dict[str, pathlib.Path] = {}
    for pair in spec.split(":"):
        if not pair:
            continue
        alias, sep, path = pair.partition("=")
        if not sep or not alias or not path:
            _fatal(f"malformed --roots entry: {pair!r}")
            return None
        roots[alias] = pathlib.Path(path)
    return roots


def _split_relpath(relpath: str) -> tuple[str, str] | None:
    """state / quarantine 的 relpath 是 `alias/子路径` 形态——按首个 `/` 切分。
    没有 `/`（缺子路径）⇒ 返回 None。"""
    alias, sep, subpath = relpath.partition("/")
    if not sep or not alias or not subpath:
        return None
    return alias, subpath


def check_source(
    state: str,
    roots_spec: str,
    out_exclude_dir: str,
    quarantine: str | None = None,
    quarantine_reason: str | None = None,
) -> int:
    """spec §6.3.1 step 13b。返回 0/3/1（见模块 docstring）。"""
    if bool(quarantine) != bool(quarantine_reason):
        _fatal("--quarantine and --quarantine-reason must be provided in pairs")
        return 1

    roots = _parse_roots(roots_spec)
    if roots is None:
        return 1

    exclude: dict[str, set[str]] = {alias: set() for alias in roots}

    quarantined: set[tuple[str, str]] = set()
    if quarantine:
        for entry in quarantine.split(","):
            entry = entry.strip()
            if not entry:
                continue
            split = _split_relpath(entry)
            if split is None:
                _fatal(f"malformed --quarantine entry (want alias/subpath): {entry!r}")
                return 1
            alias, subpath = split
            if alias not in roots:
                _fatal(f"--quarantine references unknown root alias: {alias!r}")
                return 1
            quarantined.add((alias, subpath))

    has_anomaly = False
    if state != "NONE":
        try:
            records = cass_common.state_read(state)
        except (OSError, cass_common.StateCorrupt) as exc:
            _fatal(f"failed to read --state {state!r}: {exc}")
            return 1

        for rec in records:
            if rec.status == "absent_at_source":
                continue  # 源端本来就没有——无从比对，见模块 docstring
            split = _split_relpath(rec.relpath)
            if split is None:
                _fatal(f"state record has malformed relpath (want alias/subpath): {rec.relpath!r}")
                return 1
            alias, subpath = split
            if alias not in roots:
                _fatal(f"state record references unknown root alias: {alias!r}")
                return 1
            if (alias, subpath) in quarantined:
                continue  # 人工点名放行——不比对、不计入异常

            src_path = roots[alias] / subpath
            if not src_path.exists():
                continue  # 此刻源端也没有——同上，比对不了

            size = src_path.stat().st_size
            if size < rec.nas_size:
                exclude[alias].add(subpath)
                has_anomaly = True
                continue

            prefix_hash = cass_common.blake3_file(src_path, prefix_len=rec.nas_size)
            if prefix_hash != rec.blake3:
                exclude[alias].add(subpath)
                has_anomaly = True

    # 人工点名的文件无条件进 exclude（即便它此刻源端校验其实会过——用户已经显式
    # 决定「不管它，别传」）。放在状态检查之后统一并入，保证即便 state==NONE
    # 也照样生效。
    for alias, subpath in quarantined:
        exclude[alias].add(subpath)

    out_dir = pathlib.Path(out_exclude_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for alias in roots:
        exclude_path = out_dir / f"exclude.{alias}"
        body = "".join(f"/{subpath}\n" for subpath in sorted(exclude[alias]))
        exclude_path.write_text(body, encoding="utf-8")

    return 3 if has_anomaly else 0


def parse_itemize(in_path: str) -> tuple[int, list[str]]:
    """spec step 13d。返回 `(exit_code, transferred_relpaths)`；`exit_code != 0`
    时第二个元素为空列表（fail-closed：宁可什么都不产出，也不能让调用方误用一份
    只解析到一半的清单）。"""
    transferred: list[str] = []
    with open(in_path, "r", encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.rstrip("\n")
            if line == "":
                continue
            if line.startswith(">f"):
                code, sep, path = line.partition(" ")
                if not sep or not path:
                    _fatal(f"malformed itemize line {lineno} (missing path field): {raw_line!r}")
                    return 1, []
                transferred.append(path)
            elif line.startswith("cd") or line.startswith(".d") or line.startswith(".f"):
                continue
            else:
                _fatal(f"unknown itemize line {lineno} (fail-closed): {raw_line!r}")
                return 1, []
    return 0, transferred


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cass_sessions.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check-source", help="spec §6.3.1 step 13b 源端前缀校验")
    p_check.add_argument("--state", required=True, help="共享状态文件路径，或字面量 NONE（首晚）")
    p_check.add_argument("--roots", required=True, help="alias=path:alias=path...")
    p_check.add_argument("--out-exclude-dir", required=True, dest="out_exclude_dir")
    p_check.add_argument("--quarantine", default=None, help="逗号分隔的 alias/子路径 列表")
    p_check.add_argument("--quarantine-reason", default=None, dest="quarantine_reason")

    p_itemize = sub.add_parser("parse-itemize", help="spec step 13d rsync -ai 输出分流")
    p_itemize.add_argument("--in", required=True, dest="in_path")

    args = parser.parse_args(argv)

    if args.command == "check-source":
        return check_source(
            args.state, args.roots, args.out_exclude_dir,
            args.quarantine, args.quarantine_reason,
        )

    if args.command == "parse-itemize":
        rc, transferred = parse_itemize(args.in_path)
        if rc == 0:
            for path in transferred:
                print(path)
        return rc

    parser.error(f"unknown command: {args.command!r}")  # pragma: no cover — argparse 已挡住
    return 2


if __name__ == "__main__":
    sys.exit(main())
