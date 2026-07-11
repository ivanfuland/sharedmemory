"""`infra/backup/cass/cass_chain.py` 的测试（Task 15，spec §8.3 sidecar 链校验
算法逐字）。

覆盖 Task 15 brief 的场景（`verify_chain(dest, keep) -> list[str]`，空=PASS）：

  - V15a：三连晚 `prev_sidecar_sha256` 逐对成立 → PASS；改一份 digest.json → FAIL
  - V15b：rebaseline 晚含 `rebaselined_from`+reason → PASS；改成别的目录名 →
    FAIL；缺 reason → FAIL
  - V15c：`touch` 改 mtime → 仍 PASS；删保留集中间一份 → FAIL（后继落 B 但非
    最老）
  - V15d：`jq .` 式重排某 digest.json（语义等价字节不同）→ FAIL
  - V15f：`g>=KEEP && |R|==1` → FAIL；反例：只验指针规则的劣化版会误判 PASS
    （劣化对照写在本文件内，不是生产代码的一部分）
  - V15h：同 V15f 场景但最新一份带 `rebaselined_from`/`adopt_reason` 留痕 →
    仍 FAIL（例外不豁免计数下界）
  - V15i：`retention_reset` 缺 reason 拒绝；带它的必须是链头；generation 不
    重置；`n<KEEP` 与 `n>=KEEP` 两种下界各一例
  - V15j：两次连续 rebaseline / adopt 与 rebaseline 同夜 / 首次基线 → 三种都
    正确
  - 边界：空 DEST（R 空）→ FAIL（「no published backups」）；单份首晚（gen1
    prev 空）→ PASS
  - 读不到 digest 的 R 成员必须 FAIL 而非 skip（与 `cass_common` 轮转的宽容
    skip 语义相反——本文件不复用 `_iter_published`）
  - 与真实产物集成一测：跑两晚真备份（`run_backup`）→ `verify_chain` PASS
    （钉住 `make_fake_backup` 与真产物的字段一致性）
  - CLI：exit 0/1 与 `--dest`/`--keep`
  - Review 修复轮钉子（两个 reviewer 实际构造并验证过的绕过）：
    · 离路 retention_reset（gens {3,5,6,7,9}、KEEP=7、reset@g5 不在走查路径上）
      → FAIL 指认 reset 非链头——遍历环内的 C1 链头检查对没被走到的节点不触发，
      下界改算必须独立再断言 `r_name == head_name`；
    · 孤儿/分叉（g1←g2 与 g1←g3，g3.prev 直指 g1 绕过 g2，|R| 恰好等于下界）
      → FAIL 指认孤儿——B 终止时必须断言走查路径覆盖全部合法成员
      （spec §8.3「无分叉、无缺环」）
  - generation 重复 → FAIL（spec「generation 重复/非正 int → FAIL」）

本文件自包含，不跨文件 import 其它测试文件的私有函数（同代码库既有约定）。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

import cass_common
import cass_chain

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
CHAIN_SCRIPT = REPO / "infra" / "backup" / "cass" / "cass_chain.py"

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd"
)


# ---------------------------------------------------------------------------
# 帮手：make_fake_backup（brief 指名的 helper）+ 三连晚链构造 + CLI 调用
# ---------------------------------------------------------------------------


def make_fake_backup(dest, name, gen, prev_name="", prev_sha="", **extra) -> pathlib.Path:
    """只造链校验需要的最小形状：`COMPLETE` + 含
    `generation`/`prev_backup_name`/`prev_sidecar_sha256`（+ 任意 `extra` 键，
    常用于塞 `rebaselined_from`/`reason`/`retention_reset`/
    `retention_reset_reason`/`adopt_reason`）的 `digest.json`。用
    `cass_common.dumps_canonical` 写（确定性字节——链哈希才有意义），`touch`
    `COMPLETE`。不跑全脚本。"""
    backup_dir = pathlib.Path(dest) / name
    backup_dir.mkdir(parents=True)
    digest: dict = {
        "backup_name": name,
        "generation": gen,
        "prev_backup_name": prev_name,
        "prev_sidecar_sha256": prev_sha,
    }
    digest.update(extra)
    (backup_dir / "digest.json").write_bytes(cass_common.dumps_canonical(digest))
    (backup_dir / "COMPLETE").touch()
    return backup_dir


def _chain_of_3(dest) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """三个含正确 sha256 链式指针的正常发布晚（generation 1-3）。"""
    b1 = make_fake_backup(dest, "cass-n1", gen=1)
    sha1 = cass_common.sha256_file(b1 / "digest.json")
    b2 = make_fake_backup(dest, "cass-n2", gen=2, prev_name="cass-n1", prev_sha=sha1)
    sha2 = cass_common.sha256_file(b2 / "digest.json")
    b3 = make_fake_backup(dest, "cass-n3", gen=3, prev_name="cass-n2", prev_sha=sha2)
    return b1, b2, b3


def _run_cli(dest, keep) -> tuple[int, str, str]:
    result = subprocess.run(
        [str(VENV_PY), str(CHAIN_SCRIPT), "--dest", str(dest), "--keep", str(keep)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# V15a：三连晚正常链 PASS；改一份 digest.json 内容 → FAIL
# ---------------------------------------------------------------------------


def test_v15a_three_night_chain_passes(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    _chain_of_3(dest)

    assert cass_chain.verify_chain(dest, keep=7) == []


def test_v15a_tampered_digest_fails(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    b1, b2, b3 = _chain_of_3(dest)

    # 改 n2 的 digest.json 内容（塞一个无关字段）——n2 自身的 sha256 因此改变，
    # n3 记录的 prev_sidecar_sha256（发布时算的 n2 原始字节）从此对不上。
    tampered = json.loads((b2 / "digest.json").read_bytes())
    tampered["tampered_marker"] = "codex-attack"
    (b2 / "digest.json").write_bytes(cass_common.dumps_canonical(tampered))

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("prev_sidecar_sha256" in p and "cass-n3" in p for p in problems), problems


# ---------------------------------------------------------------------------
# V15b：rebaseline 晚 —— rebaselined_from+reason PASS；改目标/缺 reason FAIL
# ---------------------------------------------------------------------------


def test_v15b_rebaseline_with_reason_passes(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-base", gen=1)
    make_fake_backup(
        dest, "cass-rb", gen=2,
        prev_name="cass-base", prev_sha="irrelevant-under-C2",
        rebaselined_from="cass-base", reason="合法迁移，schema_version 20→21",
    )

    assert cass_chain.verify_chain(dest, keep=7) == []


def test_v15b_rebaselined_from_mismatch_fails(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-base", gen=1)
    make_fake_backup(dest, "cass-other", gen=2)
    make_fake_backup(
        dest, "cass-rb", gen=3,
        prev_name="cass-base", prev_sha="",
        rebaselined_from="cass-other",  # != prev_backup_name（cass-base）
        reason="伪造：rebaselined_from 指向别的目录",
    )

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("rebaselined_from" in p and "cass-rb" in p for p in problems), problems


def test_v15b_rebaseline_missing_reason_fails(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-base", gen=1)
    make_fake_backup(
        dest, "cass-rb", gen=2,
        prev_name="cass-base", prev_sha="irrelevant-under-C2",
        rebaselined_from="cass-base",  # 缺 reason
    )

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("reason" in p and "cass-rb" in p for p in problems), problems


# ---------------------------------------------------------------------------
# V15c：touch 改 mtime 仍 PASS；删保留集中间一份 → FAIL（后继落 B 但非最老）
# ---------------------------------------------------------------------------


def test_v15c_touch_mtime_still_passes(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    b1, b2, b3 = _chain_of_3(dest)

    future = b1.stat().st_mtime + 10_000
    for p in (b1, b1 / "COMPLETE", b1 / "digest.json"):
        os.utime(p, (future, future))

    assert cass_chain.verify_chain(dest, keep=7) == []


def test_v15c_delete_middle_fails(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    b1, b2, b3 = _chain_of_3(dest)
    shutil.rmtree(b2)

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("cass-n3" in p and "cass-n2" in p for p in problems), problems


# ---------------------------------------------------------------------------
# V15d：jq . 式重排（json.load 后 indent=2 重写，语义等价字节不同）→ FAIL
# ---------------------------------------------------------------------------


def test_v15d_jq_style_reformat_fails(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    b1, b2, b3 = _chain_of_3(dest)

    parsed = json.loads((b2 / "digest.json").read_bytes())
    reformatted = json.dumps(parsed, indent=2).encode("utf-8")
    assert reformatted != (b2 / "digest.json").read_bytes(), "前置条件：重排后字节必须确实不同"
    (b2 / "digest.json").write_bytes(reformatted)

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("prev_sidecar_sha256" in p and "cass-n3" in p for p in problems), problems


# ---------------------------------------------------------------------------
# V15f：g>=KEEP && |R|==1 → FAIL；反例：只验指针规则的劣化版会误判 PASS
# ---------------------------------------------------------------------------


def _degraded_pointer_only_verdict(dest) -> bool:
    """劣化对照（brief 明确要求写在测试里，不是生产代码）：只验证 C1/C2/A/B
    指针链走查，完全不看计数下界。用来证明「只有指针规则」拦不住『删到只剩
    一份，却把 generation 造得很大』这种事故——必须叠加计数下界才是
    `cass_chain.verify_chain` 的真实防线（spec §8.3 的原文论证）。"""
    dest = pathlib.Path(dest)
    entries: dict[str, dict] = {}
    for entry in sorted(dest.glob("cass-*")):
        if not entry.is_dir() or not (entry / "COMPLETE").exists():
            continue
        digest = cass_common.read_digest(entry)
        if not digest or "generation" not in digest:
            continue
        entries[entry.name] = digest
    if not entries:
        return True
    head = min(entries, key=lambda n: entries[n]["generation"])
    tip = max(entries, key=lambda n: entries[n]["generation"])
    cur = tip
    visited: set[str] = set()
    while True:
        if cur in visited:
            return False
        visited.add(cur)
        prev = entries[cur].get("prev_backup_name", "")
        if prev and prev in entries:
            cur = prev
            continue
        return cur == head  # 情况 B：只检查「是否最老」，从不看 KEEP


def test_v15f_low_count_high_generation_fails(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-lone", gen=10)  # g=10 >= KEEP=7，但 |R|==1

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("计数下界" in p for p in problems), problems

    # 反例：劣化版（只验指针，不验计数）在这个场景下会误判 PASS——证明
    # 计数下界不是可有可无的装饰，是真实防线。
    assert _degraded_pointer_only_verdict(dest) is True


# ---------------------------------------------------------------------------
# V15h：同 V15f 场景 + 最新一份带 rebaselined_from/adopt_reason 留痕 → 仍 FAIL
# ---------------------------------------------------------------------------


def test_v15h_low_count_with_rebaseline_marker_still_fails(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(
        dest, "cass-lone", gen=10,
        prev_name="cass-vanished", prev_sha="",
        rebaselined_from="cass-vanished", reason="看起来合法的 rebaseline 留痕",
    )

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != [], "rebaseline 留痕不豁免计数下界"
    assert any("计数下界" in p for p in problems), problems


def test_v15h_low_count_with_adopt_reason_still_fails(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-lone", gen=10, adopt_reason="sessions channel adopt bootstrap")

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != [], "adopt 留痕不豁免计数下界"
    assert any("计数下界" in p for p in problems), problems


# ---------------------------------------------------------------------------
# V15i：retention_reset —— 缺 reason / 非链头 拒绝；n<KEEP 与 n>=KEEP 各一例
# （两例都用「重置点后 generation 继续递增，不重置回 1」的链，覆盖 brief
# 「generation 不重置」条目）
# ---------------------------------------------------------------------------


def test_v15i_retention_reset_missing_reason_fails(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-reset", gen=5, retention_reset=True)  # 缺 reason

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("retention_reset" in p and "reason" in p for p in problems), problems


def test_v15i_retention_reset_not_head_fails(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    # cass-older（generation=1）仍在保留集里，比 retention_reset 的 generation=5
    # 还老——retention_reset 因此不合法（它必须是 R 中最老者才能当链头）。
    make_fake_backup(dest, "cass-older", gen=1)
    make_fake_backup(
        dest, "cass-reset", gen=5,
        retention_reset=True, retention_reset_reason="手动清了旧的，但漏删 cass-older",
    )

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("链头" in p and "cass-reset" in p for p in problems), problems


def test_v15i_retention_reset_n_below_keep_passes(tmp_path):
    """n = g - r + 1 = 3 < KEEP(7)：下界应按 n 算（==3），不是按 KEEP 算
    （若按 g=7 >= KEEP=7 的朴素规则，会误要求 |R|==7 而 FAIL——这正是
    retention_reset 存在的意义）。generation 从 5 继续递增到 7，不重置回 1。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    b5 = make_fake_backup(
        dest, "cass-r5", gen=5,
        retention_reset=True, retention_reset_reason="磁盘紧张，手动清了 5 之前的",
    )
    sha5 = cass_common.sha256_file(b5 / "digest.json")
    b6 = make_fake_backup(dest, "cass-r6", gen=6, prev_name="cass-r5", prev_sha=sha5)
    sha6 = cass_common.sha256_file(b6 / "digest.json")
    make_fake_backup(dest, "cass-r7", gen=7, prev_name="cass-r6", prev_sha=sha6)

    assert cass_chain.verify_chain(dest, keep=7) == []


def test_v15i_retention_reset_n_at_or_above_keep_passes(tmp_path):
    """n = g - r + 1 = 3 >= KEEP(3)：下界封顶在 KEEP（==3），不是按 n 算。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    b2 = make_fake_backup(
        dest, "cass-r2", gen=2,
        retention_reset=True, retention_reset_reason="手动清了 2 之前的",
    )
    sha2 = cass_common.sha256_file(b2 / "digest.json")
    b3 = make_fake_backup(dest, "cass-r3", gen=3, prev_name="cass-r2", prev_sha=sha2)
    sha3 = cass_common.sha256_file(b3 / "digest.json")
    make_fake_backup(dest, "cass-r4", gen=4, prev_name="cass-r3", prev_sha=sha3)

    assert cass_chain.verify_chain(dest, keep=3) == []


# ---------------------------------------------------------------------------
# V15j：两次连续 rebaseline / adopt 与 rebaseline 同夜 / 首次基线 —— 三种都正确
# ---------------------------------------------------------------------------


def test_v15j_two_consecutive_rebaselines_pass(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-base", gen=1)
    make_fake_backup(
        dest, "cass-rb1", gen=2,
        prev_name="cass-base", prev_sha="irrelevant-under-C2",
        rebaselined_from="cass-base", reason="第一次 rebaseline",
    )
    make_fake_backup(
        dest, "cass-rb2", gen=3,
        prev_name="cass-rb1", prev_sha="irrelevant-under-C2",
        rebaselined_from="cass-rb1", reason="第二次 rebaseline（连续）",
    )

    assert cass_chain.verify_chain(dest, keep=7) == []


def test_v15j_adopt_and_rebaseline_same_night_passes(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-base", gen=1)
    make_fake_backup(
        dest, "cass-combo", gen=2,
        prev_name="cass-base", prev_sha="irrelevant-under-C2",
        rebaselined_from="cass-base", reason="迁移 + 同夜 adopt",
        adopt_reason="sessions channel 同夜收编",
    )

    assert cass_chain.verify_chain(dest, keep=7) == []


def test_v15j_first_baseline_passes(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-first", gen=1)  # prev_backup_name 为空

    assert cass_chain.verify_chain(dest, keep=7) == []


# ---------------------------------------------------------------------------
# 边界：空 DEST；单份首晚
# ---------------------------------------------------------------------------


def test_empty_dest_fails_with_no_published_backups(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("no published backups" in p for p in problems), problems


def test_single_first_night_passes(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-onlyone", gen=1)

    assert cass_chain.verify_chain(dest, keep=7) == []


# ---------------------------------------------------------------------------
# 读不到 digest 的 R 成员必须 FAIL 而非 skip（与 cass_common 轮转的宽容 skip
# 语义相反——本模块自己实现遍历，见模块 docstring）
# ---------------------------------------------------------------------------


def test_unreadable_digest_in_r_fails_not_skipped(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-good", gen=1)

    bad = dest / "cass-badgen"
    bad.mkdir()
    (bad / "COMPLETE").touch()
    (bad / "digest.json").write_bytes(b"{not valid json")

    problems = cass_chain.verify_chain(dest, keep=7)
    # 若是宽容 skip（像轮转那样），剩下的 cass-good 单独看是合法首晚，会误判 PASS。
    # 链校验必须把「R 里有个成员读不到 digest」本身当一条 FAIL 问题报出来。
    assert problems != []
    assert any("cass-badgen" in p for p in problems), problems


def test_missing_digest_json_in_complete_dir_fails_not_skipped(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-good", gen=1)

    bare = dest / "cass-nodigest"
    bare.mkdir()
    (bare / "COMPLETE").touch()  # 无 digest.json

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("cass-nodigest" in p for p in problems), problems


def test_non_dict_digest_in_r_fails_not_skipped(tmp_path):
    """whole-branch review 修复项：`digest.json` 是合法 JSON 但裸标量（如 `5`）
    ——`"generation" not in digest` 对 int 会 TypeError。归入 FAIL 语义（与坏
    JSON 同，见 `_scan_r` docstring），不是 `cass_common` 轮转扫描那种宽容 skip。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-good", gen=1)

    scalar = dest / "cass-scalardigest"
    scalar.mkdir()
    (scalar / "COMPLETE").touch()
    (scalar / "digest.json").write_bytes(cass_common.dumps_canonical(5))

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("cass-scalardigest" in p for p in problems), problems


# ---------------------------------------------------------------------------
# Review 修复轮钉子 #1：离路 retention_reset 不得降低计数下界（reviewer 构造 A2）
# ---------------------------------------------------------------------------


def test_off_path_retention_reset_does_not_lower_bound(tmp_path):
    """gens {3,5,6,7,9}、KEEP=7：主链 g9→A→g7→A→g6→A→g3 经 B 终止在 g3（链头），
    从不路过带 retention_reset 的 g5——遍历环内的 C1 链头检查因此永不触发。
    若下界改算无条件采用 max-gen reset holder 的 r，期望值会从 KEEP=7 被静默
    降到 n = 9-5+1 = 5，恰好 |R|==5 → 假 PASS，g8 的删除被掩盖。修复后必须
    FAIL 且指认 reset 非链头（外加孤儿指认——g5 也不在走查路径上）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    b3 = make_fake_backup(dest, "cass-a3", gen=3)
    make_fake_backup(
        dest, "cass-a5", gen=5,
        retention_reset=True, retention_reset_reason="离路伪造：不在 tip→head 路径上",
    )
    sha3 = cass_common.sha256_file(b3 / "digest.json")
    b6 = make_fake_backup(dest, "cass-a6", gen=6, prev_name="cass-a3", prev_sha=sha3)
    sha6 = cass_common.sha256_file(b6 / "digest.json")
    b7 = make_fake_backup(dest, "cass-a7", gen=7, prev_name="cass-a6", prev_sha=sha6)
    sha7 = cass_common.sha256_file(b7 / "digest.json")
    make_fake_backup(dest, "cass-a9", gen=9, prev_name="cass-a7", prev_sha=sha7)

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != [], "离路 retention_reset 不得把下界从 KEEP 降到 n"
    assert any(
        "retention_reset" in p and "链头" in p and "cass-a5" in p for p in problems
    ), problems


# ---------------------------------------------------------------------------
# Review 修复轮钉子 #2：孤儿/分叉逃逸（reviewer 构造 A1，spec §8.3「无分叉/无缺环」）
# ---------------------------------------------------------------------------


def test_orphan_fork_off_main_path_fails(tmp_path):
    """g1←g2 与 g1←g3 两条边（g3.prev 直指 g1、sha256 正确，绕过 g2）：走查
    g3→A→g1 经 B 合法终止在链头 g1，g2 从不被任何一跳校验触及；且 g=3 < KEEP=7
    ⇒ expected=|R|=3，计数下界也放行。修复前假 PASS；修复后 B 终止时必须断言
    走查路径覆盖全部合法成员 → FAIL 指认孤儿 g2。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    b1 = make_fake_backup(dest, "cass-f1", gen=1)
    sha1 = cass_common.sha256_file(b1 / "digest.json")
    make_fake_backup(dest, "cass-f2", gen=2, prev_name="cass-f1", prev_sha=sha1)
    make_fake_backup(dest, "cass-f3", gen=3, prev_name="cass-f1", prev_sha=sha1)

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != [], "分叉（g2 孤儿不在 tip→head 路径上）必须 FAIL"
    assert any("cass-f2" in p and ("孤儿" in p or "分叉" in p) for p in problems), problems


# ---------------------------------------------------------------------------
# Review 修复轮钉子 #3：C1 终止的对称孤儿逃逸（与 #2 同族，spec §8.3「无分叉/
# 无缺环」的 C1 侧——首轮 report 遗留观察，控制器确认为真缺口）
# ---------------------------------------------------------------------------


def test_c1_termination_post_reset_orphan_fails(tmp_path):
    """g5(reset,链头)←g6（孤儿，prev=g5 sha 正确）与 g5←g7←g8 主链（g7.prev
    直指 g5 绕过 g6）：走查 g8→A→g7→A→g5 经 C1 合法终止（reset 在链头、
    reason 非空、r_name==head），g6 从不被任何一跳校验触及；n = 8-5+1 = 4 ==
    |R|，计数下界也放行。修复前假 PASS；修复后 C1 终止时必须断言 gen >= r 的
    成员全部在走查路径上 → FAIL 指认 post-reset 孤儿 g6。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    b5 = make_fake_backup(
        dest, "cass-c5", gen=5,
        retention_reset=True, retention_reset_reason="合法 reset，但重置点之后链分叉了",
    )
    sha5 = cass_common.sha256_file(b5 / "digest.json")
    make_fake_backup(dest, "cass-c6", gen=6, prev_name="cass-c5", prev_sha=sha5)  # 孤儿
    b7 = make_fake_backup(dest, "cass-c7", gen=7, prev_name="cass-c5", prev_sha=sha5)
    sha7 = cass_common.sha256_file(b7 / "digest.json")
    make_fake_backup(dest, "cass-c8", gen=8, prev_name="cass-c7", prev_sha=sha7)

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != [], "post-reset 孤儿在 C1 终止时必须被指认"
    assert any("cass-c6" in p and ("孤儿" in p or "分叉" in p) for p in problems), problems


# ---------------------------------------------------------------------------
# codex R4-P1：C2（rebaseline）终止分支的孤儿/分叉逃逸——R1 修 B/C1 孤儿时漏的
# 第三个终止分支。rebaseline 点之后（gen >= r_c2）的成员必须全在走查路径上。
# ---------------------------------------------------------------------------


def test_c2_termination_post_rebaseline_fork_fails(tmp_path):
    """codex 复现：g1←g2(rebaselined_from=g1)，g3(prev=g2 sha 正确)+g4(prev=g2
    sha 正确, tip) 两条边从 g2 分叉。走查 g4→A→g2 经 C2 合法终止（rebaselined_from
    ==prev、reason 非空），g3 从不被任何一跳校验触及；g=4 < KEEP=7 ⇒ expected=
    |R|=4，计数下界也放行。修复前假 PASS；修复后 C2 终止时必须断言 gen >= 重置点
    的成员全在走查路径上 → FAIL 指认 post-rebaseline 孤儿 g3。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-g1", gen=1)
    b2 = make_fake_backup(
        dest, "cass-g2", gen=2,
        prev_name="cass-g1", prev_sha="irrelevant-under-C2",
        rebaselined_from="cass-g1", reason="合法 rebaseline，但之后链分叉了",
    )
    sha2 = cass_common.sha256_file(b2 / "digest.json")
    make_fake_backup(dest, "cass-g3", gen=3, prev_name="cass-g2", prev_sha=sha2)  # 孤儿
    make_fake_backup(dest, "cass-g4", gen=4, prev_name="cass-g2", prev_sha=sha2)  # tip

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != [], "post-rebaseline 分叉（g3 不在 tip→rebaseline 点路径上）必须 FAIL"
    assert any("cass-g3" in p and ("孤儿" in p or "分叉" in p) for p in problems), problems


def test_c2_termination_no_fork_still_passes(tmp_path):
    """对照回归：rebaseline 点之后是线性链（g2(rebaseline)←g3←g4(tip)，无分叉）
    → C2 孤儿检查不误伤，PASS。rebaseline 之前的 g1（gen < 重置点）允许不在走查
    路径上——那正是 rebaseline 关掉旧历史比对的语义。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-g1", gen=1)
    b2 = make_fake_backup(
        dest, "cass-g2", gen=2,
        prev_name="cass-g1", prev_sha="irrelevant-under-C2",
        rebaselined_from="cass-g1", reason="合法 rebaseline，之后线性推进",
    )
    sha2 = cass_common.sha256_file(b2 / "digest.json")
    b3 = make_fake_backup(dest, "cass-g3", gen=3, prev_name="cass-g2", prev_sha=sha2)
    sha3 = cass_common.sha256_file(b3 / "digest.json")
    make_fake_backup(dest, "cass-g4", gen=4, prev_name="cass-g3", prev_sha=sha3)

    assert cass_chain.verify_chain(dest, keep=7) == []


# ---------------------------------------------------------------------------
# generation 重复 → FAIL（spec「generation 重复/非正 int → FAIL」）
# ---------------------------------------------------------------------------


def test_duplicate_generation_fails(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-dup-a", gen=2)
    make_fake_backup(dest, "cass-dup-b", gen=2)

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems != []
    assert any("generation 重复" in p for p in problems), problems


# ---------------------------------------------------------------------------
# 与真实产物集成一测：跑两晚真备份 → verify_chain PASS
# ---------------------------------------------------------------------------


def _write_verified_doctor_stub(home: pathlib.Path, manifests_dir: pathlib.Path) -> None:
    import cass_manifest_census

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


def _run_real_backup(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp):
    _write_verified_doctor_stub(tmp_home, synth_dd / "raw-mirror" / "v1" / "manifests")
    env = {
        "CASS_DATA_DIR": str(synth_dd),
        "CASS_BACKUP_DEST": str(dest),
        "CASS_BACKUP_STAGING": str(staging),
        "CASS_BACKUP_STAMP": stamp,
        "CASS_BACKUP_ADOPT_SESSIONS": "1",
        "CASS_BACKUP_ADOPT_REASON": "test fixture — chain verification not sessions channel",
        "PATH": f"{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    rc, out, _dest = run_backup(env=env)
    return rc, out


@requires_cass
def test_real_two_night_backup_passes_chain_verification(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"

    rc1, out1 = _run_real_backup(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "night1")
    assert rc1 == 0, out1
    rc2, out2 = _run_real_backup(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "night2")
    assert rc2 == 0, out2

    problems = cass_chain.verify_chain(dest, keep=7)
    assert problems == [], problems


# ---------------------------------------------------------------------------
# CLI：exit 0/1
# ---------------------------------------------------------------------------


def test_cli_exit_0_on_pass(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    _chain_of_3(dest)

    rc, out, err = _run_cli(dest, keep=7)
    assert rc == 0, f"stdout={out}\nstderr={err}"
    assert "PASS" in out


def test_cli_exit_1_on_fail_with_problems_printed(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    make_fake_backup(dest, "cass-lone", gen=10)

    rc, out, err = _run_cli(dest, keep=7)
    assert rc == 1, f"stdout={out}\nstderr={err}"
    assert "FAIL" in out
    assert "计数下界" in out
