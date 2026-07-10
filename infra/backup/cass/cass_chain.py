"""CASS 备份 PR1 sidecar 链校验（spec §8.3 算法逐字翻译）。

链的目的（spec §8.3 原文）：`census.tsv`/`digest.json` 与 db 同源同权限，本设计
**不对抗主动篡改者**——它只能发现「没把整条链一起改」的低级损坏（位腐、误删、
半写）。每份 `digest.json` 记 `prev_backup_name`/`prev_sidecar_sha256`（上一份
`digest.json` 字节的 sha256）+ 单调递增的 `generation`，形成历史连续性可验证的
链。本模块实现该校验的可执行定义（`verify_chain`），供每周通道（spec §6.5）与
PR2 restore 前置复用同一函数——接口故意保持干净：`verify_chain(dest, keep) ->
list[str]`，空列表 = PASS，非空 = FAIL（每条是一个具体问题）。

与 `cass_common._iter_published`/`rotation_victims` 的关键差异：那两个函数对
「读不到 digest 内容」（缺 digest.json / 坏 JSON / 缺 generation 键 / generation
非 int）是**宽容跳过**语义（轮转候选，跳过不影响其余轮转判断）。链校验不是——
保留集 R 的定义是「所有含 COMPLETE 的 cass-*/ 目录」，若其中某个成员的 digest
读不到，那本身就是一处完整性问题，必须在返回的问题列表里体现（FAIL），不能
静默把它从 R 里摘掉当作没发生。因此本模块自己实现遍历，不复用
`_iter_published`。目录探测层（`is_dir`/`COMPLETE` 存在性）的 OS 级错误仍照
`_iter_published` 同一约定不包 try——DEST 权限坏是环境事件，必须响亮失败
（上抛），不是「链坏了」这一类判定。

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
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cass_common  # noqa: E402 — 同目录 import 约定见模块 docstring


def _scan_r(dest: pathlib.Path) -> tuple[dict[str, dict], list[str], list[str]]:
    """扫 `dest` 下所有含 `COMPLETE` 的 `cass-*/` 目录（保留集 R 的定义，spec
    §8.3：「R = 所有含 COMPLETE 的 cass-*/ 目录」——SUSPECT-*/INCOMPLETE-* 不属于
    R）。返回 `(valid, problems, all_names)`：

    - `valid`：digest 可读且 `generation` 合法（正 int）的成员，`{name: digest}`。
    - `problems`：digest 读不到 / 坏 JSON / 缺 `generation` 键 / `generation`
      非正 int 的成员各生成一条问题（FAIL，不是 skip——见模块 docstring）。这些
      成员不进 `valid`，因为拿不到 generation/指针，没法参与后续链算法。
    - `all_names`：R 的完整名字列表（不论 digest 是否可读），用于区分「R 真的
      是空的」（no published backups）与「R 有成员但 digest 全部读不到」。

    目录探测层（`is_dir`/`COMPLETE` 存在性）刻意不包 try：`PermissionError` 等
    OS 级错误照常上抛，与 `cass_common._iter_published` 同一约定。
    """
    valid: dict[str, dict] = {}
    problems: list[str] = []
    all_names: list[str] = []
    for entry in sorted(dest.glob("cass-*")):
        if not entry.is_dir():
            continue
        if not (entry / "COMPLETE").exists():
            continue
        all_names.append(entry.name)
        try:
            digest = cass_common.read_digest(entry)
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(
                f"{entry.name}: digest.json 读取失败（{type(exc).__name__}: {exc}）"
            )
            continue
        if digest is None:
            problems.append(f"{entry.name}: 缺 digest.json")
            continue
        if "generation" not in digest:
            problems.append(f"{entry.name}: digest.json 缺 generation 字段")
            continue
        generation = digest["generation"]
        if type(generation) is not int or generation <= 0:
            problems.append(f"{entry.name}: generation 非法（{generation!r}，必须是正整数）")
            continue
        valid[entry.name] = digest
    return valid, problems, all_names


def verify_chain(dest, keep: int) -> list[str]:
    """spec §8.3 链校验算法的可执行定义。空列表 = PASS，非空 = FAIL（每条一个
    具体问题，逐条打印供人读）。

    步骤（对应 spec §8.3 代码块，逐段翻译）：
    1. R = 所有含 COMPLETE 的 cass-*/ 目录；digest 读不到的成员各记一条问题
       （FAIL，见 `_scan_r`）。R 完全为空（无这类目录）单独报「无备份可言」。
    2. `generation` 重复检测（「generation 重复/非正 int → FAIL」，非正已在
       `_scan_r` 拦，这里补重复）。
    3. 从 generation 最大者（tip）沿 `prev_backup_name` 逐跳走到链头，每跳按
       C1（retention_reset）> C2（rebaselined_from）> A（指针+sha256）> B（仅
       最老者豁免）优先级判定，先命中先生效。C1/C2 命中即终止本次走查（不再
       深入 P——「允许链断开」）；C1 额外要求 cur 必须是 R 中最老者；A 要求
       sha256(P/digest.json 字节) == cur.prev_sidecar_sha256 精确匹配，不符终止
       并记问题；B 只有 cur 是 R 中最老者时才合法终止。
    4. 计数下界：g = max(generation)；有 retention_reset 时按 n = g - r + 1
       （r = 带该字段者的最大 generation）改算下界，否则按 g 与 KEEP 比较。
       adopt/rebaseline 不改变这条下界（spec §8.3「不豁免」）。
    """
    dest = pathlib.Path(dest)
    valid, problems, all_names = _scan_r(dest)

    if not all_names:
        return ["R 为空（DEST 下没有任何含 COMPLETE 的 cass-*/ 目录）— no published backups"]
    if not valid:
        # all_names 非空但 valid 为空：R 里每个成员都已经在 problems 里留了
        # 一条读不到 digest 的具体原因，不再额外补「无备份」这类误导性文案。
        return problems

    # generation 重复检测（非正 int 已在 _scan_r 拦）。重复时 tip/head（max/min
    # by generation）的选取失去良定义，继续走链/算下界只会产出误导性的次生问题
    # ——已经确定 FAIL，就此返回。
    gen_counts = Counter(d["generation"] for d in valid.values())
    dup_gens = sorted(g for g, c in gen_counts.items() if c > 1)
    if dup_gens:
        problems.append(f"generation 重复: {dup_gens}")
        return problems

    tip_name = max(valid, key=lambda n: valid[n]["generation"])
    head_name = min(valid, key=lambda n: valid[n]["generation"])

    # ------------------------------------------------------------------
    # 从 tip 沿 prev_backup_name 走到链头（C1 > C2 > A > B，先命中先生效）。
    # ------------------------------------------------------------------
    visited: set[str] = set()
    cur = tip_name
    while True:
        if cur in visited:
            problems.append(f"环路：{cur} 在链走查中被再次访问")
            break
        visited.add(cur)
        digest = valid[cur]

        if digest.get("retention_reset"):
            # C1（最高优先级）：允许 prev 指针断开，但必须带非空 reason 且
            # cur 必须是链头（R 中最老者）。
            reason = digest.get("retention_reset_reason")
            if not reason:
                problems.append(f"{cur}: retention_reset 缺非空 retention_reset_reason")
            if cur != head_name:
                problems.append(
                    f"{cur}: 带 retention_reset 但不是链头（R 中最老者应为 {head_name}）"
                )
            # 与 B 终止的孤儿检查同族（spec §8.3「无分叉、无缺环」的 C1 侧）：
            # C1 合法终止时「早于 r 的允许不在 R」，但 gen >= r 的成员必须全部
            # 在 tip→重置点的走查路径上——post-reset 孤儿可让 |R|==n 恰好成立
            # 而逃逸（如 g5(reset)←g6 孤儿 + g8→g7→g5 主链绕过 g6：n=4==|R|，
            # 计数下界放行）。gen < r 的成员若存在则不在此指认：|R| > n 让计数
            # 下界必 FAIL，且 reset 不再是最老者、上面的链头检查也已触发。
            r_cur = digest["generation"]
            orphans = sorted(
                m for m in set(valid) - visited if valid[m]["generation"] >= r_cur
            )
            if orphans:
                problems.append(
                    f"孤儿/分叉：成员 {orphans} 的 generation >= 重置点 {r_cur}，"
                    f"却不在 tip({tip_name})→重置点({cur}) 的走查路径上"
                )
            break

        if "rebaselined_from" in digest:
            # C2：允许链断开，但 rebaselined_from 必须 == cur.prev_backup_name，
            # 缺 reason ⇒ FAIL。不要求 cur 是链头（可以是链中间的一次 rebaseline）。
            rebaselined_from = digest["rebaselined_from"]
            prev_field = digest.get("prev_backup_name", "")
            reason = digest.get("reason")
            if rebaselined_from != prev_field:
                problems.append(
                    f"{cur}: rebaselined_from={rebaselined_from!r} 必须等于 "
                    f"prev_backup_name={prev_field!r}"
                )
            if not reason:
                problems.append(f"{cur}: rebaselined_from 缺非空 reason")
            break

        prev_field = digest.get("prev_backup_name", "")
        if prev_field and prev_field in valid:
            # A：P ∈ R，必须 sha256(P/digest.json 字节) == cur.prev_sidecar_sha256。
            actual_sha256 = cass_common.sha256_file(dest / prev_field / "digest.json")
            expected_sha256 = digest.get("prev_sidecar_sha256")
            if actual_sha256 != expected_sha256:
                problems.append(
                    f"{cur}: prev_sidecar_sha256 不匹配（记录={expected_sha256!r} "
                    f"实际={actual_sha256!r}，前驱={prev_field}）"
                )
                break
            cur = prev_field
            continue

        # B：P ∉ R（目录不存在 / 无 COMPLETE / 指向 SUSPECT-*/INCOMPLETE-*/
        # RECOVERABLE-* / 读不到 digest 因而不在 valid 里）。仅当 cur 是 R 中
        # 最老者时允许（链头）；否则 FAIL。
        if cur == head_name:
            # spec §8.3「无分叉、无环、无缺环」：B 终止 = 正常走通到链头。此时
            # tip→head 的路径必须覆盖 R 的全部合法成员——否则未覆盖者是孤儿/
            # 分叉（如 g3.prev 直指 g1 绕过 g2：g2 从此不被任何一跳校验触及，
            # 而 |R| 又可能恰好等于计数下界 → 假 PASS，reviewer 实际构造过）。
            # 只在 B 终止时检查：C1/C2 是合法断链（链头语义 / rebaseline），
            # 断点之前的成员允许留在 R 却不在走查路径上（V15b/V15j 场景）。
            orphans = sorted(set(valid) - visited)
            if orphans:
                problems.append(
                    f"孤儿/分叉：成员 {orphans} 不在 tip({tip_name})→链头({head_name}) 的走查路径上"
                )
            break
        problems.append(
            f"{cur}: 前驱 {prev_field!r} 不在保留集内，且 {cur} 不是链头（应为 {head_name}）"
        )
        break

    # ------------------------------------------------------------------
    # 计数下界（spec §8.3：「删到只剩最新一份」若没有这条会被当成正常轮转）。
    # adopt/rebaseline 都不豁免；唯一能改动下界的是 retention_reset。
    # ------------------------------------------------------------------
    g = max(d["generation"] for d in valid.values())
    reset_holders = [
        (name, d["generation"]) for name, d in valid.items() if d.get("retention_reset")
    ]
    if reset_holders:
        r_name, r = max(reset_holders, key=lambda t: t[1])
        # 独立于链走查的再断言（spec §8.3「带它的那份必须是链头」）：C1 的链头
        # 检查在遍历环内，只覆盖被走到的节点——若生效 reset 持有者离路（走查经
        # B 在别处终止，从不路过它），那条检查永远不触发，而这里的下界改算却会
        # 无条件采用它的 r，把「必须 |R|==KEEP」静默降成 n → 删除掩盖后门
        # （reviewer 实际构造过：gens {3,5,6,7,9}、KEEP=7、reset@g5 离路）。
        if r_name != head_name:
            problems.append(
                f"{r_name}: 带 retention_reset（生效重置点）但不是链头（R 中最老者应为 {head_name}）"
            )
        n = g - r + 1
        expected = keep if n >= keep else n
        bound_desc = f"retention_reset 生效点={r_name}(generation={r})，n={n}"
    else:
        expected = keep if g >= keep else g
        bound_desc = f"g={g}"

    # 不变式说明：spec 的 |R| 按定义是 len(all_names)（所有含 COMPLETE 的
    # cass-*/）。这里用 len(valid)——两者不等时（有成员 digest 读不到），
    # `_scan_r` 已为每个差额成员各记了一条 FAIL 问题，整体判定已经是 FAIL，
    # 本条下界的具体数字只影响报文不影响判定；两者相等时（正常情况）就是 |R|。
    if len(valid) != expected:
        problems.append(
            f"计数下界不满足：{bound_desc}，KEEP={keep}，期望 |R|=={expected}，实际 |R|=={len(valid)}"
        )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cass_chain.py")
    parser.add_argument("--dest", required=True, help="已发布备份的 DEST 根目录")
    parser.add_argument("--keep", required=True, type=int, help="keep-N 轮转的 N")
    args = parser.parse_args(argv)

    problems = verify_chain(args.dest, args.keep)
    if not problems:
        print("[chain] PASS")
        return 0

    print("[chain] FAIL:")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
