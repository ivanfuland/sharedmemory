"""M0 minimal 6-role -> EverOS /add three-shape mapping (spec §5.1 faithful subset).

M1 adds: append-to-preceding, orphan coalesce, pure-orphan-cell quarantine,
naive cap / RTK compression, incremental watermark.
"""

from __future__ import annotations

import json

_KNOWN = {"user", "assistant", "tool_call", "tool_result", "reasoning", "system"}


def _args_str(v) -> str:
    """EverOS ToolCallDTO.arguments 必须是 str；非 str 转 JSON。"""
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def _synthetic_assistant(content: str, agent_id: str, ts: int) -> dict:
    return {"sender_id": agent_id, "role": "assistant", "timestamp": ts, "content": content}


def map_to_add_messages(msgs: list[dict], agent_id: str, user_sender: str) -> list[dict]:
    out: list[dict] = []
    for m in msgs:
        role = m.get("role")
        content = m.get("content") or ""
        ts = int(m.get("timestamp"))
        ex = m.get("extra_json") or {}

        if role == "user":
            out.append({"sender_id": user_sender, "role": "user", "timestamp": ts, "content": content})
        elif role == "assistant":
            out.append({"sender_id": agent_id, "role": "assistant", "timestamp": ts, "content": content})
        elif role == "tool_call":
            out.append(
                {
                    "sender_id": agent_id,
                    "role": "assistant",
                    "timestamp": ts,
                    "content": content or f"[Tool: {ex.get('tool_name', '')}]",
                    "tool_calls": [
                        {
                            "id": ex.get("tool_call_id", ""),
                            "type": "function",
                            "function": {
                                "name": ex.get("tool_name", ""),
                                "arguments": _args_str(ex.get("args", "")),
                            },
                        }
                    ],
                }
            )
        elif role == "tool_result":
            # unpaired 绝不当 role=tool 传：EverOS 只认 role=="tool" and tool_call_id，
            # 否则落 fall-through raise（docstring 明写 "no orphan tool rows"）。
            if ex.get("unpaired") or not ex.get("tool_call_id"):
                out.append(
                    _synthetic_assistant(f"[tool_result:{ex.get('tool_name', '')}] {content}", agent_id, ts)
                )
            else:
                out.append(
                    {
                        "sender_id": agent_id,
                        "role": "tool",
                        "timestamp": ts,
                        "content": content,
                        "tool_call_id": ex["tool_call_id"],
                    }
                )
        elif role == "reasoning":
            # EverOS 无此 role → synthetic assistant。绝不并入 user（会污染 user owner）。
            out.append(_synthetic_assistant(f"[reasoning] {content}", agent_id, ts))
        elif role == "system":
            continue  # 配置噪声，drop
        # 真·未知 role：M0 保守 skip（真库里有 gemini/info/error 这类野 role）
    return out
