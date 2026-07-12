"""编排：clamp -> redact（硬闸，/add 前最后一道）-> 分批 -> /add -> /flush。

脱敏**必须在 clamp 之后**（spec §5.3）：压完的最终文本再脱一遍，保证无未脱敏文本溜出。
只压 `role=tool` 的 content；user/assistant 散文忠实保留。
"""

from __future__ import annotations

import httpx

from cass_corpus.redact import redact_secrets
from everos_adapter.batching import into_batches
from everos_adapter.cap import Clamper

APP_ID, PROJECT_ID = "default", "default"


# 被降级成 synthetic assistant 的 tool 消息仍携带完整未截断的 tool 输出，
# 必须一并过 cap（真实 tool_result 均长 3421）。
_CAPPABLE_PREFIXES = ("[tool_result]", "[tool_call]")


def _needs_cap(m: dict) -> bool:
    if m.get("role") == "tool":
        return True
    return (
        m.get("role") == "assistant"
        and not m.get("tool_calls")
        and (m.get("content") or "").startswith(_CAPPABLE_PREFIXES)
    )


def _sanitize(m: dict, clamper: Clamper, tool_result_cap: int) -> dict:
    """**redact -> clamp -> redact**（codex R2-1，实测坐实）。

    只做 `clamp -> redact` 有真实漏洞：clamp **切断** secret 时（`_cap_line` 的关键词
    居中截窗会在行中间切），redact 的正则不再命中，输出残留 `sk-ABC123S` 这样的片段，
    违反 §5.3「无未脱敏文本溜出」。
    - **前置 redact**：让 clamp 看到的已是脱敏文本，切断 `[REDACTED_SECRET]` 无害。
    - **后置 redact**：clamp 会从被丢弃的中段抢救出硬错误行，那些内容需再过一次闸。
    `redact_secrets` 已实测幂等，双次调用安全。
    """
    m = dict(m)
    content = m.get("content")
    if isinstance(content, str):
        content = redact_secrets(content)                       # 前置
        if _needs_cap(m):
            content = clamper.clamp(content, tool_result_cap)
            content = redact_secrets(content)                   # 后置
        m["content"] = content
    if m.get("tool_calls"):
        m["tool_calls"] = [
            {
                **tc,
                "function": {**tc["function"], "arguments": redact_secrets(tc["function"].get("arguments", ""))},
            }
            for tc in m["tool_calls"]
        ]
    return m


def feed_session(
    base_url: str,
    session_id: str,
    mapped: list[dict],
    clamper: Clamper,
    tool_result_cap: int = 1500,
) -> dict:
    b = base_url.rstrip("/")
    prepared = [_sanitize(m, clamper, tool_result_cap) for m in mapped]

    adds = []
    for batch in into_batches(prepared):
        r = httpx.post(
            f"{b}/api/v1/memory/add",
            json={"session_id": session_id, "app_id": APP_ID, "project_id": PROJECT_ID, "messages": batch},
            timeout=120,
        )
        r.raise_for_status()
        adds.append(r.json())

    flush = httpx.post(
        f"{b}/api/v1/memory/flush",
        json={"session_id": session_id, "app_id": APP_ID, "project_id": PROJECT_ID},
        timeout=300,
    )
    flush.raise_for_status()
    return {"add": adds, "flush": flush.json()}
