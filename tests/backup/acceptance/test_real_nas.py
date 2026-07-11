"""Tier B acceptance — NAS/CIFS 机制验收（spec §6.1/§6.4/§9.2 V7a/V9/V14c，
marker `slow nas`）。`CASS_ACCEPT_NAS=1` 才跑（`tests/backup/acceptance/conftest.py`
在收集阶段 skip，本文件不重复判断）。

全部在 `~/nas/openclaw/backups/.cass-accept-<pid>/` scratch 目录（`nas_scratch`
fixture）内操作，绝不碰生产 `~/nas/openclaw/backups/cass/`——唯一例外是最后一个
测试，对生产遗留的 `agent_search.db.pre-franken-*` 只做只读 `stat`/`glob`。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import time

import pytest

import cass_common

pytestmark = [pytest.mark.slow, pytest.mark.nas]

_PROBE_FILE_MB = 256


# ---------------------------------------------------------------------------
# V7a 的 CIFS 机理反例：裸 mv 到已存在且非空目标 —— 静默嵌套，exit 0。
# mv -T 在同场景下必须拒绝。spec §6.1：「实测（CIFS上）：目标目录已存在且非空时，
# 裸 mv 不报错，而是把源目录塞进去变成嵌套」。
# ---------------------------------------------------------------------------


def test_bare_mv_nests_silently_with_exit0_while_mv_dash_t_rejects(nas_scratch):
    # 反例：裸 mv。
    target = nas_scratch / "cass-bare"
    target.mkdir()
    (target / "OLD-MARKER").write_text("old content\n")
    source = nas_scratch / "incomplete-bare"
    source.mkdir()
    (source / "COMPLETE").touch()

    result = subprocess.run(["mv", str(source), str(target)], capture_output=True, text=True)
    assert result.returncode == 0, (
        "反例断言前提：裸 mv 在本 CIFS 挂载上应静默成功（exit 0）——"
        f"若此断言本身失败，说明 CIFS 行为已变化，spec §6.1 的前提需要人工复核。"
        f"stderr={result.stderr}"
    )
    # 顶层目标目录仍是旧内容、COMPLETE 不在顶层——「静默成功但本轮什么都没备成」。
    assert (target / "OLD-MARKER").exists()
    assert not (target / "COMPLETE").exists()
    # 源目录被嵌套进目标里。
    nested = target / source.name
    assert nested.is_dir()
    assert (nested / "COMPLETE").exists()

    # 正例：mv -T 必须拒绝同一场景。
    target2 = nas_scratch / "cass-t"
    target2.mkdir()
    (target2 / "OLD-MARKER-2").write_text("old content 2\n")
    source2 = nas_scratch / "incomplete-t"
    source2.mkdir()
    (source2 / "COMPLETE").touch()

    result2 = subprocess.run(["mv", "-T", str(source2), str(target2)], capture_output=True, text=True)
    assert result2.returncode != 0, "mv -T 面对已存在且非空的目标必须拒绝（非零 exit）"
    assert "not empty" in result2.stderr.lower() or "Directory not empty" in result2.stderr
    # 目标未被破坏，源未被移动（两者原封不动）。
    assert (target2 / "OLD-MARKER-2").exists()
    assert not (target2 / "COMPLETE").exists()
    assert source2.is_dir()
    assert (source2 / "COMPLETE").exists()


# ---------------------------------------------------------------------------
# V9 的机理：O_DIRECT 读回必须显著慢于（绕过页缓存）刚写完的缓存读（判据「明显
# 下降」，倍数>2 即可，spec §6.4 实测 8.7GB/s vs 98.9MB/s——本测试只要求 2 倍量级
# 的稳健判据，不要求复现具体数字）。
# ---------------------------------------------------------------------------


def test_o_direct_readback_significantly_slower_than_cached_read(nas_scratch):
    test_file = nas_scratch / "odirect-probe.bin"
    write_result = subprocess.run(
        ["dd", "if=/dev/urandom", f"of={test_file}", "bs=1M", f"count={_PROBE_FILE_MB}", "status=none"],
        capture_output=True, text=True, timeout=120,
    )
    assert write_result.returncode == 0, write_result.stderr
    os.sync()

    # 缓存读：刚写完，走 CIFS 客户端页缓存，应该很快。
    t0 = time.monotonic()
    cached = subprocess.run(
        ["dd", f"if={test_file}", "of=/dev/null", "bs=1M", "status=none"],
        capture_output=True, text=True, timeout=60,
    )
    cached_elapsed = time.monotonic() - t0
    assert cached.returncode == 0, cached.stderr

    # O_DIRECT 读：绕过页缓存，强制真实网络读。
    t0 = time.monotonic()
    direct = subprocess.run(
        ["dd", f"if={test_file}", "of=/dev/null", "bs=1M", "iflag=direct", "status=none"],
        capture_output=True, text=True, timeout=120,
    )
    direct_elapsed = time.monotonic() - t0
    assert direct.returncode == 0, direct.stderr

    assert direct_elapsed > cached_elapsed * 2, (
        f"O_DIRECT 读回应显著慢于刚写完的缓存读（判据：>2倍），实测 "
        f"direct={direct_elapsed:.3f}s cached={cached_elapsed:.3f}s —— 若不再成立，"
        f"说明 CIFS 挂载参数（cache=strict 等）已变化，需人工复核 spec §6.4 的前提"
    )


# ---------------------------------------------------------------------------
# V14c 的 NAS 半句：posix_fadvise(DONTNEED) 效果同判据——调用后强制丢弃页缓存，
# 紧接着的读显著慢于「不调用 fadvise 的连续读」。
# ---------------------------------------------------------------------------


def test_fadvise_dontneed_forces_cache_bypass(nas_scratch):
    test_file = nas_scratch / "fadvise-probe.bin"
    write_result = subprocess.run(
        ["dd", "if=/dev/urandom", f"of={test_file}", "bs=1M", f"count={_PROBE_FILE_MB}", "status=none"],
        capture_output=True, text=True, timeout=120,
    )
    assert write_result.returncode == 0, write_result.stderr
    os.sync()

    # 先正常读一次，确保数据确实在客户端缓存里（比单纯依赖"写后即缓存"更稳）。
    warm_hash = cass_common.blake3_file(test_file, fadvise=False)

    t0 = time.monotonic()
    cached_hash = cass_common.blake3_file(test_file, fadvise=False)
    cached_elapsed = time.monotonic() - t0
    assert cached_hash == warm_hash

    t0 = time.monotonic()
    fadvised_hash = cass_common.blake3_file(test_file, fadvise=True)
    fadvised_elapsed = time.monotonic() - t0
    assert fadvised_hash == warm_hash

    assert fadvised_elapsed > cached_elapsed * 2, (
        f"fadvise(DONTNEED) 应强制丢弃页缓存、走真实网络读，显著慢于紧接着的缓存读"
        f"（判据：>2倍），实测 fadvise={fadvised_elapsed:.3f}s cached={cached_elapsed:.3f}s"
    )


# ---------------------------------------------------------------------------
# 生产遗留 pre-franken 快照：只 stat/glob，不修改/删除；证明轮转 glob（`cass-*`）
# 天然不匹配它（spec §7/§11：「既有 agent_search.db.pre-franken-* 一律不得被删」）。
# 不依赖 nas_scratch（本测试只读生产目录，不需要 scratch 隔离）。
# ---------------------------------------------------------------------------


def test_pre_franken_snapshot_exists_and_rotation_glob_does_not_match_it():
    prod_cass_dir = pathlib.Path.home() / "nas" / "openclaw" / "backups" / "cass"
    if not prod_cass_dir.is_dir():
        pytest.skip(f"生产 cass/ 目录不存在：{prod_cass_dir}")

    candidates = sorted(prod_cass_dir.glob("agent_search.db.pre-franken-*"))
    candidates = [p for p in candidates if not p.name.endswith(("-shm", "-wal"))]
    if not candidates:
        pytest.skip("生产环境当前没有 pre-franken 快照文件（可能已被人工清理，属现场变化）")
    pre_franken = candidates[0]

    assert pre_franken.is_file(), "只 stat，不修改/删除该文件"
    stat_result = pre_franken.stat()
    assert stat_result.st_size > 0

    # 轮转 glob 只匹配 `cass-*` 前缀（见 infra/backup/cass/cass_common.py
    # `_iter_published` 内部用的正是这个 glob）——`agent_search.db.pre-franken-`
    # 前缀天然不落入其中，不需要任何额外排除逻辑。
    rotation_glob_matches = set(prod_cass_dir.glob("cass-*"))
    assert pre_franken not in rotation_glob_matches
    assert pre_franken.name not in {p.name for p in rotation_glob_matches}
