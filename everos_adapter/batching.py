"""分批 + 单调唯一时间戳（spec §5.4）。

EverOS 的 `message_id` = `m_<session>_<ts>_<idx>`，`idx` 每批重置 -> 同 ts 消息跨页可撞。
故给一会话内消息赋单调唯一 ts（next-free-ms）。**任何输入都不丢消息**——即便数百条消息
共享同一 ms-epoch，或时间戳乱序，一律归一化为严格递增、互不相同的 `int > 0` ts 后原样喂入。
不存在「skew 超限 quarantine」这类会整会话丢弃的机制（2026-07-13 项目决策：不丢数据，永远排队入）。

DTO 硬约束（实测）：`messages` <=500/批；`timestamp: int > 0`。
"""

from __future__ import annotations

MAX_BATCH = 500


def ensure_unique_timestamps(msgs: list[dict]) -> list[dict]:
    """ts<=0 视为「reader 未取到 created_at」-> 顺延填补。

    reader 绝不回落 idx+1（会与 ms epoch 混用量纲，codex R0 P0#3）。缺失值统一在此填补。
    无论输入时间戳如何分布（大量重复 / 乱序 / 缺失），每条消息都会被赋予一个严格递增、
    互不相同的 `int > 0` ts 并保留在输出中——本函数永不抛出、永不丢弃消息。
    """
    out: list[dict] = []
    last = 0  # DTO 要求 ts > 0，故从 0 起，首个至少被抬到 1
    for m in msgs:
        m = dict(m)
        ts = int(m.get("timestamp") or 0)
        if ts <= 0:
            want = last + 1              # 缺失 ts：顺延
        else:
            want = max(ts, last + 1)
        m["timestamp"] = want
        last = want
        out.append(m)
    return out


def into_batches(msgs: list[dict], size: int = MAX_BATCH) -> list[list[dict]]:
    if size < 1:
        raise ValueError(f"batch size must be >= 1, got {size}")
    if size > MAX_BATCH:
        raise ValueError(f"EverOS MessageItemDTO list max_length={MAX_BATCH}")
    return [msgs[i : i + size] for i in range(0, len(msgs), size)]
