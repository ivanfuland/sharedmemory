"""M0: feed one synthetic session into EverOS (redact -> /add -> /flush).

脱敏是**硬闸**（spec §5.3）：EverOS 把喂进去的内容当 markdown 真源**落盘存住**，
且 data_dir 计划备份到 NAS/异地 —— 密钥不脱 = 持久躺库 + 异地泄漏。
故 `/add` 前必过 `redact_secrets`，不是推荐项。即便是合成 fixture 也走这道闸。
"""

from __future__ import annotations

import json
import pathlib

import httpx

from cass_corpus.redact import redact_secrets  # 复用生产脱敏（14 正则）
from everos_m0.role_map import map_to_add_messages

APP_ID, PROJECT_ID = "default", "default"
MAX_BATCH = 500  # EverOS MessageItemDTO list max_length（实测 DTO）


def _redact_msg(m: dict) -> dict:
    """content + tool_calls[].function.arguments 双路径脱敏。

    tool args 是常被忽略的泄漏面：`curl -H 'Authorization: Bearer ...'`、DSN 都藏在这。
    """
    m = dict(m)
    if isinstance(m.get("content"), str):
        m["content"] = redact_secrets(m["content"])
    if m.get("tool_calls"):
        m["tool_calls"] = [
            {
                **tc,
                "function": {
                    **tc["function"],
                    "arguments": redact_secrets(tc["function"].get("arguments", "")),
                },
            }
            for tc in m["tool_calls"]
        ]
    return m


def feed_session(base_url: str, session_id: str, mapped: list[dict]) -> dict:
    b = base_url.rstrip("/")
    if len(mapped) > MAX_BATCH:
        raise ValueError(f"M0 只喂单批；{len(mapped)} > {MAX_BATCH}（分批是 M1 的事）")

    redacted = [_redact_msg(m) for m in mapped]
    add = httpx.post(
        f"{b}/api/v1/memory/add",
        json={
            "session_id": session_id,
            "app_id": APP_ID,
            "project_id": PROJECT_ID,
            "messages": redacted,
        },
        timeout=60,
    )
    add.raise_for_status()
    flush = httpx.post(
        f"{b}/api/v1/memory/flush",
        json={"session_id": session_id, "app_id": APP_ID, "project_id": PROJECT_ID},
        timeout=120,
    )
    flush.raise_for_status()
    return {"add": add.json(), "flush": flush.json()}


if __name__ == "__main__":
    fix = json.loads((pathlib.Path(__file__).parent / "fixtures/synthetic_session.json").read_text())
    mapped = map_to_add_messages(fix["messages"], agent_id="ivan-coding", user_sender="ivan")
    print(json.dumps(feed_session("http://127.0.0.1:8000", fix["session_id"], mapped), ensure_ascii=False, indent=2))
