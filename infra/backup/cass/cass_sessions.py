"""CASS 备份 PR1 sessions 通道 A/B —— 源端前缀校验 / itemize 解析（通道 A，spec
§6.3.1 数据流 step 13b-13d）+ 共享状态增量回读 / 发布门全量回读 / ADOPT（通道
B，step 13e-13g）。

四个子命令：

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

`update-state`（13e）：rsync 一返回就跑——对 `--transferred` 点名的**已传输**文件
从 `--sessions-root` 回读内容重算 size/blake3，写入/覆盖对应记录（status 恒
`present`，因为刚从磁盘读到了字节）；其余既有记录原样结转。不存在的 `--state`
从空清单起（bash 层的「首晚需要 ADOPT」政策判断在 13a，不在本函数）。它的发布**不
依赖本次备份后续是否成功**——`state_write_atomic` 单文件原子发布，13e 解析漏记的
文件由 13f 的全量回读自愈（见下），不是本函数的职责。

`publish-gate`（13f/13g）：发布前对清单**每一条**记录 + `--sessions-root` 下物理
存在的**每一个**文件做全量读回校验（先 `posix_fadvise(DONTNEED)`，绝不只 `stat`）。
`--sessions-root` 下有清单没有的文件，按**本轮 `--transferred` 集合**分流——
∈ transferred 视为 13e 的自愈对象（漏记，读回收编）；∉ transferred 视为无关的
receiver-only 文件（无 `--adopt` 即 FAIL，给了则收编 + stdout 留痕）。这个二分
是硬约束：**没有 transferred 集合就无法区分这两类**，任何单一规则都会打破其中一
个方向的验收（codex R3-P1）。任一判据 FAIL ⇒ 不写任何东西（state/out-tsv 都不
动，现场保留）；全过 ⇒ 原子重写 `--state`，把同样字节拷进 `--out-tsv`。

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
    共享的已知边界，不在本 task 范围内解决）。**alias 约定**：禁 `/`（state/
    quarantine 的 relpath 按首个 `/` 切分 alias）、禁 `|`（bash 侧
    `sed "s|^|$alias/|"` 用它当定界符）、禁 `,`（quarantine 列表按逗号切分）；
    生产只用三个固定 alias（claude-projects / codex-sessions / openclaw-agents），
    其它形态不是预期输入。格式非法 ⇒ 返回 None。"""
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


def _subpath_has_linebreak(subpath: str) -> bool:
    """`check_source` 产出的 exclude 文件是行式格式（一行一个 `/`-锚定的相对路
    径）。子路径本身若含裸 `\\n`/`\\r`，写进去会破坏这个行式结构（要么把自己拆
    成两条伪造的 exclude 行，要么在 `\\r` 处截断真实那一行）——这是该机制本身的
    边界，不是可以事后修补的细节，必须在源头 fail-closed。"""
    return "\n" in subpath or "\r" in subpath


# rsync exclude 模式是 glob，不是字面路径——`*`/`?`/`[` 会被当通配符解释。
# 实测（rsync 3.2.7）：exclude 文件裸写 `/s[1].jsonl` 时 `[1]` 被当字符类，
# 真名叫 `s[1].jsonl` 的文件**照样被传输**——排除保证（§6.3.1 核心）当场失效。
# 逐字符 bracket-escape（`[*]`/`[?]`/`[[]`/`[]]`）后 rsync 按字面匹配。
_RSYNC_GLOB_CHAR_ESCAPE = {"*": "[*]", "?": "[?]", "[": "[[]", "]": "[]]"}
# 触发转义的判定字符集：`*`/`?`/`[` 任一出现 ⇒ 该模式在 rsync 眼里含通配符。
# 孤立的 `]`（没有配对 `[`）不是通配符，无需触发。
_RSYNC_GLOB_TRIGGER = ("*", "?", "[")


def _escape_rsync_glob(subpath: str) -> str:
    """把 exclude 行里的文件路径逐字符转义成「rsync 按字面匹配」的 glob 模式。

    两个 regime（rsync wildmatch 的实测行为，两边都有测试钉住）：

    - 路径不含任何通配符字符（`*`/`?`/`[`）⇒ **原样返回**。rsync 规则：模式里
      没有通配符时整串按字面比较，反斜杠也是字面——此时任何「转义」反而会引入
      通配符语义、破坏匹配。
    - 含通配符 ⇒ 逐字符映射：`*`/`?`/`[`/`]` 换成单字符 bracket 类；`\\` 必须
      同时翻倍成 `\\\\`——一旦模式含通配符，rsync 就把 `\\` 当转义字符解释，
      裸 `\\` 会吃掉下一个字符（实测：`back\\slash[[]2[]].jsonl` 不翻倍时排除
      失效，翻倍后正确排除）。
    """
    if not any(ch in subpath for ch in _RSYNC_GLOB_TRIGGER):
        return subpath
    return "".join(
        "\\\\" if ch == "\\" else _RSYNC_GLOB_CHAR_ESCAPE.get(ch, ch) for ch in subpath
    )


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
            if _subpath_has_linebreak(subpath):
                _fatal(f"--quarantine subpath contains a line break (would corrupt exclude file): {subpath!r}")
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
            if _subpath_has_linebreak(subpath):
                _fatal(f"state record subpath contains a line break (would corrupt exclude file): {subpath!r}")
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
        # 前导 `/` 锚定 root 相对路径；路径本身必须过 glob 转义（见
        # `_escape_rsync_glob`——裸写含 `[`/`*`/`?` 的文件名会让 rsync 把它当
        # 通配符，排除保证失效）。
        body = "".join(f"/{_escape_rsync_glob(subpath)}\n" for subpath in sorted(exclude[alias]))
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


def _read_transferred(path: str) -> list[str] | None:
    """`--transferred` 是每行一个 `alias/子路径` 的清单（bash 侧
    `$STG/transferred.all`，由 13d 的 `parse-itemize` 输出逐 root 加前缀拼成）。
    空行忽略。读失败（文件不存在等）返回 None，调用方转成内部错误。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f if line.strip()]
    except OSError as exc:
        _fatal(f"failed to read --transferred {path!r}: {exc}")
        return None


def update_state(state: str, sessions_root: str, transferred_path: str) -> int:
    """spec §6.3.1 step 13e：rsync 一返回就更新共享状态——只回读**本次传输过的**
    文件，其余记录原样结转；`state_write_atomic` 单次 `mv -T` 原子发布。**不等
    本次备份成功**（调用方在 rsync 成功后立刻调用本函数，无论后续 13f/g 或五腿
    门是否通过）。

    `--state` 指向的文件不存在（首晚，或 state 已被 ADOPT 通道显式重建过）⇒ 从
    空清单起——本函数不做「首晚需要 ADOPT」的政策判断，那是 bash 层 13a 的职责
    （见 backup-cass.sh 注释）；本函数只关心「有没有这个文件」这一件事。

    返回 0 = 已发布；1 = 内部错误（state 损坏、`--transferred` 读不到、清单点名
    的文件此刻在 `--sessions-root` 下读不到）。
    """
    state_path = pathlib.Path(state)
    if state_path.exists():
        try:
            existing = cass_common.state_read(state_path)
        except (OSError, cass_common.StateCorrupt) as exc:
            _fatal(f"failed to read --state {state!r}: {exc}")
            return 1
    else:
        existing = []

    by_relpath: dict[str, cass_common.SessionRec] = {r.relpath: r for r in existing}

    transferred = _read_transferred(transferred_path)
    if transferred is None:
        return 1

    sessions_root_path = pathlib.Path(sessions_root)
    for relpath in transferred:
        file_path = sessions_root_path / relpath
        if not file_path.exists():
            _fatal(
                f"transferred entry has no file at --sessions-root (rc=fail-closed): {relpath!r}"
            )
            return 1
        size = file_path.stat().st_size
        content_hash = cass_common.blake3_file(file_path, fadvise=True)
        by_relpath[relpath] = cass_common.SessionRec(relpath, size, content_hash, "present")

    records = [by_relpath[relpath] for relpath in sorted(by_relpath)]
    kill_before_replace = os.environ.get("CASS_BACKUP_FAULT") == "kill-before-state-publish"
    cass_common.state_write_atomic(state_path, records, _kill_before_replace=kill_before_replace)
    return 0


def publish_gate(
    state: str,
    sessions_root: str,
    roots_spec: str,
    transferred_path: str,
    out_tsv: str,
    adopt: bool = False,
    adopt_reason: str | None = None,
) -> int:
    """spec §6.3.1 step 13f/13g：发布门——对清单**每一条**记录 + NAS 上物理存在
    的每一个文件做全量读回校验（先 `posix_fadvise(DONTNEED)`），然后原子重写权
    威 state + 把对账结果拷进 `--out-tsv`（`.incomplete-<stamp>/sessions.tsv`）。

    判据（逐条对应 spec §6.3.1 第 2 点 + task 12 brief 的 R3-P1 binding）：

    - 清单记录在 `--sessions-root` 下找不到对应文件 ⇒ **FAIL**（V12l），**不分
      `present` / `absent_at_source`**——spec §6.3.1 发布门原文「对清单里的每一
      条记录从 NAS 读回内容重算 blake3：文件不存在 ⇒ FAIL」没有状态豁免；且
      §6.5 反面教训①点名「源端已删除、NAS 仍保留的会话（absent_at_source）正是
      Tier 0′ 最该保住的」——源端已没了，NAS 是最后一份，丢它必须响。
    - 记录存在且文件存在：内容全量比对——
        - size/hash 都与记录相符 ⇒ 通过，原样结转（状态可能因源端消失而降级，见下）。
        - 不符：仅当「向前漂移」（NAS 更长 **且** `blake3(NAS[0:记录.nas_size]) ==
          记录.hash`）⇒ 修正记录（V12m/V12i/V12j 的落脚点）；否则 ⇒ **FAIL**（V14/
          V12g/V12h）。
    - `--sessions-root` 下物理存在、清单里**没有记录**的文件 ⇒ 按**本轮
      `--transferred` 集合**分流（codex R3-P1 binding——没有这个集合就无法区分
      下面两类，任何单一规则都会打破 V12f 或 V12k2 之一）：
        - relpath ∈ transferred ⇒ 本轮 13e 的自愈：本该被 `update-state` 记录却
          漏记了（如 `drop-one-itemize`），读回收编（V12k2）。
        - relpath ∉ transferred ⇒ 预先存在、与本轮同步无关的 receiver-only 文
          件；`--adopt` 未给 ⇒ **FAIL**（fail-closed，V12f）；给了则收编 + 留痕。
    - 每条记录（含刚验证/修正/收编的）：`--roots` 下对应源文件此刻是否存在决定
      最终 `present`/`absent_at_source`（覆盖「整根源目录消失」场景——Task 11
      reviewer 留的验证项：源端没了但 NAS 内容仍必须读回验证过，验证通过后结转
      为 `absent_at_source`；NAS 也没了则是上面第一条的 FAIL，不是这一条）。

    任一 FAIL ⇒ **不写任何东西**（state 与 out-tsv 都不动，保留现场）、返回 1。
    全部通过 ⇒ 原子重写 `--state`，把同样内容拷进 `--out-tsv`，stdout 打印
    adopt/self-heal/drift 的明细行（bash 转发；Task 13 消费其留痕语义），返回 0。

    `--adopt`/`--adopt-reason` 必须成对：给了 `--adopt` 没给 reason（或反过来）
    ⇒ 内部错误，返回 1（不做任何读回，最快失败）。
    """
    if adopt != bool(adopt_reason):
        _fatal("--adopt and --adopt-reason must be provided in pairs")
        return 1

    roots = _parse_roots(roots_spec)
    if roots is None:
        return 1

    state_path = pathlib.Path(state)
    if state_path.exists():
        try:
            existing = cass_common.state_read(state_path)
        except (OSError, cass_common.StateCorrupt) as exc:
            _fatal(f"failed to read --state {state!r}: {exc}")
            return 1
    else:
        existing = []

    transferred = _read_transferred(transferred_path)
    if transferred is None:
        return 1
    transferred_set = set(transferred)

    by_relpath: dict[str, cass_common.SessionRec] = {r.relpath: r for r in existing}
    sessions_root_path = pathlib.Path(sessions_root)

    disk_relpaths: set[str] = set()
    for alias in roots:
        alias_dir = sessions_root_path / alias
        if not alias_dir.is_dir():
            continue
        for entry in alias_dir.rglob("*"):
            if entry.is_file():
                disk_relpaths.add(f"{alias}/{entry.relative_to(alias_dir).as_posix()}")

    all_relpaths = set(by_relpath) | disk_relpaths

    final_records: dict[str, cass_common.SessionRec] = {}
    adopted: list[str] = []
    self_healed: list[str] = []
    drift_corrected: list[str] = []
    problems: list[str] = []

    for relpath in sorted(all_relpaths):
        split = _split_relpath(relpath)
        if split is None:
            _fatal(f"malformed relpath (want alias/subpath): {relpath!r}")
            return 1
        alias, subpath = split
        if alias not in roots:
            _fatal(f"relpath references unknown root alias: {relpath!r}")
            return 1

        existing_rec = by_relpath.get(relpath)
        nas_path = sessions_root_path / relpath

        if not nas_path.exists():
            # 有记录、NAS 无文件 ⇒ 无条件 FAIL，不分 present / absent_at_source
            # （spec §6.3.1「文件不存在 ⇒ FAIL」无状态豁免；absent_at_source 的
            # NAS 副本是最后一份，丢它更要响——见函数 docstring 第一条判据）。
            # existing_rec is None 的分支结构上不可达：relpath ∈ all_relpaths 却
            # 两边都没有（不在 state、也不在磁盘扫描）不可能发生。
            if existing_rec is not None:
                problems.append(
                    f"recorded (status={existing_rec.status}) but missing from NAS: {relpath}"
                )
            continue

        actual_size = nas_path.stat().st_size
        actual_hash = cass_common.blake3_file(nas_path, fadvise=True)

        if existing_rec is None:
            if relpath in transferred_set:
                self_healed.append(relpath)
            elif adopt:
                adopted.append(relpath)
            else:
                problems.append(f"NAS file with no state record, not in this run's transferred set: {relpath}")
                continue
        elif existing_rec.nas_size == actual_size and existing_rec.blake3 == actual_hash:
            pass  # 内容完全相符，直接沿用
        elif actual_size > existing_rec.nas_size and cass_common.blake3_file(
            nas_path, fadvise=True, prefix_len=existing_rec.nas_size
        ) == existing_rec.blake3:
            drift_corrected.append(relpath)  # 向前漂移：NAS 更长且旧记录是其前缀
        else:
            problems.append(f"content mismatch, not forward-drift: {relpath}")
            continue

        src_exists = (roots[alias] / subpath).exists()
        final_records[relpath] = cass_common.SessionRec(
            relpath, actual_size, actual_hash, "present" if src_exists else "absent_at_source"
        )

    if problems:
        for problem in problems:
            _fatal(problem)
        return 1

    records = [final_records[relpath] for relpath in sorted(final_records)]
    # 13f 的 state 重写与 13e 的走同一个 crash 注入口子（V12n「两次写入都要
    # crash 安全」的 plumbing）：.tmp 落盘后、os.replace 前被杀 ⇒ 旧 state 原封。
    kill_before_replace = os.environ.get("CASS_BACKUP_FAULT") == "kill-before-state-publish"
    cass_common.state_write_atomic(state_path, records, _kill_before_replace=kill_before_replace)

    out_path = pathlib.Path(out_tsv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(state_path.read_bytes())

    for relpath in self_healed:
        print(f"{_PREFIX} self-healed (13e 漏记，全量回读收编): {relpath}")
    for relpath in adopted:
        print(f"{_PREFIX} adopted (reason: {adopt_reason}): {relpath}")
    for relpath in drift_corrected:
        print(f"{_PREFIX} drift-corrected (向前漂移，记录已修正): {relpath}")

    return 0


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

    p_update = sub.add_parser("update-state", help="spec §6.3.1 step 13e 共享状态增量回读")
    p_update.add_argument("--state", required=True, help="共享状态文件路径（不存在则从空清单起）")
    p_update.add_argument("--sessions-root", required=True, dest="sessions_root")
    p_update.add_argument("--transferred", required=True, dest="transferred_path")

    p_gate = sub.add_parser("publish-gate", help="spec §6.3.1 step 13f/13g 发布门全量回读")
    p_gate.add_argument("--state", required=True)
    p_gate.add_argument("--sessions-root", required=True, dest="sessions_root")
    p_gate.add_argument("--roots", required=True, help="alias=path:alias=path...")
    p_gate.add_argument("--transferred", required=True, dest="transferred_path")
    p_gate.add_argument("--out-tsv", required=True, dest="out_tsv")
    p_gate.add_argument("--adopt", action="store_true")
    p_gate.add_argument("--adopt-reason", default=None, dest="adopt_reason")

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

    if args.command == "update-state":
        return update_state(args.state, args.sessions_root, args.transferred_path)

    if args.command == "publish-gate":
        return publish_gate(
            args.state, args.sessions_root, args.roots, args.transferred_path,
            args.out_tsv, args.adopt, args.adopt_reason,
        )

    parser.error(f"unknown command: {args.command!r}")  # pragma: no cover — argparse 已挡住
    return 2


if __name__ == "__main__":
    sys.exit(main())
