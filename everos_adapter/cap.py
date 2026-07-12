"""出口截断。

**复用 `cass_corpus` 的 `_clamp`**（backend 无关，实测只 import re/sys/dataclasses/typing）。
它已被 codex 多轮打磨：reserve-upfront、扫原文整行防劈开、marker 预留保证总输出 <= cap、
关键词居中防深埋 ERROR 被截、三重封顶。重写一个必然更差。

时序：`_clamp` 当前在 `adapter/6role-denoise` 分支（swap 前置硬门，必先合 master），
而 M1b 也 gated on swap -> 它天然先于 M1b 可用。M1a 期间若尚未合并，退化为 NoopClamper。
"""

from __future__ import annotations

import sys
from typing import Protocol


class Clamper(Protocol):
    IS_TECHNICAL_DEBT: bool

    def clamp(self, content: str, cap: int) -> str: ...


class NoopClamper:
    """⚠️ 技术债：原样返回，不截断。仅供 M1a 的小合成 fixture。

    **M1b 开工前必须替换为 PrunerClamper** —— 真实会话单条 48 万字符、
    `tool_result` 均长 3421，不截断会撑爆 memcell。
    """

    IS_TECHNICAL_DEBT = True

    def clamp(self, content: str, cap: int) -> str:
        return content


class PrunerClamper:
    """委托 `cass_corpus` 的 `_clamp`（EverOS 侧只用它压 tool_result，policy 自持）。"""

    IS_TECHNICAL_DEBT = False

    def __init__(self, pruner) -> None:
        self._pruner = pruner

    def clamp(self, content: str, cap: int) -> str:
        return self._pruner._clamp(content, cap, rescue_errors=True)


def make_clamper() -> Clamper:
    """⚠️ 只捕 ImportError 是不够的（codex R0 P0#1，实测坐实）。

    master 上 `DeterministicPruner` **存在但没有 `_clamp`**（旧版仍是
    `_collapse_tool_call` / `_truncate_observation`），import 会成功，
    于是返回一个在 `clamp()` 时炸 `AttributeError` 的 PrunerClamper。
    必须显式检查 `_clamp` 是否存在。
    """
    pruner_cls = None
    try:
        from cass_corpus.pruner import DeterministicPruner

        pruner_cls = DeterministicPruner
    except ImportError:
        pass

    if pruner_cls is not None and hasattr(pruner_cls, "_clamp"):
        return PrunerClamper(pruner_cls())

    print(
        "[everos-adapter] WARNING: cass_corpus._clamp 不可用 -> NoopClamper。"
        "这是技术债，M1b 开工前必须切到 PrunerClamper（真实 tool_result 均长 3421）。",
        file=sys.stderr,
    )
    return NoopClamper()
