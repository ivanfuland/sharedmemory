"""确定性文本化策略（spec §5.1，codex R1#2 + R2#3 + R3#3/#4）。

全在**适配器自有的未喂缓冲**里做，不依赖观察 EverOS 内部 cell（那不可观察）。

- orphan 文本（`[tool_result]` / `[reasoning]` 前缀的 synthetic assistant）
  **append 到缓冲里前一条真 assistant**；无前置真 assistant 才独立成 synthetic。
- **绝不 append 到 tool_call 消息**（它的 content 属于 ToolCallRequest，污染会误导提炼器）。
- 连续 orphan **coalesce 成一条**，单条 append 有大小上限。
- **尾部纯 orphan 段不喂不 flush**（留缓冲待真锚），从源头杜绝 EverOS 形成纯孤儿 cell。
"""

from __future__ import annotations

from collections.abc import Callable

# [tool_call] 是 Task 2 对「无 id 的 tool_call」的降级产物（codex R0 P1#6），
# 与 [tool_result] / [reasoning] 同属 orphan 文本。
_ORPHAN_PREFIXES = ("[tool_result]", "[reasoning]", "[tool_call]")


def _is_orphan_text(m: dict) -> bool:
    return (
        m.get("role") == "assistant"
        and not m.get("tool_calls")
        and (m.get("content") or "").startswith(_ORPHAN_PREFIXES)
    )


def _is_real_assistant(m: dict) -> bool:
    return m.get("role") == "assistant" and not m.get("tool_calls") and not _is_orphan_text(m)


def absorb_orphans(
    mapped: list[dict],
    max_append_chars: int = 4000,
    clamp_fn: Callable[[str], str] | None = None,
) -> list[dict]:
    """codex R1 P1#1：被 append 进真 assistant 的 orphan 段，之后前缀丢失、出口 cap 检测不到，
    故必须**在 append 之前**压好。独立 synthetic 仍由出口 `_needs_cap` 兜底。
    """
    out: list[dict] = []
    for m in mapped:
        if not _is_orphan_text(m):
            out.append(dict(m))
            continue

        m = dict(m)
        if clamp_fn is not None:
            m["content"] = clamp_fn(m["content"])

        prev = out[-1] if out else None

        # 连续 orphan -> coalesce 进上一条 synthetic
        if prev is not None and _is_orphan_text(prev):
            merged = prev["content"] + "\n" + m["content"]
            if len(merged) <= max_append_chars:
                prev["content"] = merged
                continue
            out.append(m)
            continue

        # append 到前一条**真** assistant（绝不碰 tool_call 消息）
        if prev is not None and _is_real_assistant(prev):
            merged = prev["content"] + "\n" + m["content"]
            if len(merged) <= max_append_chars:
                prev["content"] = merged
                continue

        out.append(m)
    return out


def split_feedable(mapped: list[dict]) -> tuple[list[dict], list[dict]]:
    """尾部纯 orphan 段留 held，不喂不 flush。"""
    i = len(mapped)
    while i > 0 and _is_orphan_text(mapped[i - 1]):
        i -= 1
    return mapped[:i], mapped[i:]
