"""6-role -> EverOS /add 三形态映射（消费 cass_reader 的扁平输出）。

M1a 相对 M0 的变化：id / args / name 不再从 `extra_json` 取（那里恒为空壳 `{}`），
改为消费 `cass_reader` 已解包的扁平字段。unpaired 判据 = `tool_call_id` 为空串。
"""

from __future__ import annotations


def _synthetic_assistant(content: str, agent_id: str, ts: int) -> dict:
    return {"sender_id": agent_id, "role": "assistant", "timestamp": ts, "content": content}


def map_to_add_messages(msgs: list[dict], agent_id: str, user_sender: str) -> list[dict]:
    out: list[dict] = []
    for m in msgs:
        role = m.get("role")
        content = m.get("content") or ""
        ts = int(m["timestamp"])
        tcid = m.get("tool_call_id") or ""

        if role == "user":
            out.append({"sender_id": user_sender, "role": "user", "timestamp": ts, "content": content})
        elif role == "assistant":
            out.append({"sender_id": agent_id, "role": "assistant", "timestamp": ts, "content": content})
        elif role == "tool_call":
            # 无 id 的 tool_call 无法与 result 配对；空 id 的 ToolCallRequest 会污染
            # EverOS 的配对逻辑（codex R0 P1#6）。降级为文本，与 unpaired tool_result 对称。
            # 诊断书 D1 那批（1530 条 tool_call 缺 extra_bin）会走这条路。
            if not tcid:
                out.append(_synthetic_assistant(f"[tool_call] {content}", agent_id, ts))
            else:
                out.append(
                    {
                        "sender_id": agent_id,
                        "role": "assistant",
                        "timestamp": ts,
                        "content": content or f"[Tool: {m.get('tool_name', '')}]",
                        "tool_calls": [
                            {
                                "id": tcid,
                                "type": "function",
                                "function": {
                                    "name": m.get("tool_name", ""),
                                    "arguments": m.get("tool_call_args", ""),
                                },
                            }
                        ],
                    }
                )
        elif role == "tool_result":
            # 无 id 即 unpaired：EverOS 只认 role=="tool" and tool_call_id，
            # 否则落 fall-through raise（docstring 明写 "no orphan tool rows"）。
            if not tcid:
                out.append(_synthetic_assistant(f"[tool_result] {content}", agent_id, ts))
            else:
                out.append(
                    {
                        "sender_id": agent_id,
                        "role": "tool",
                        "timestamp": ts,
                        "content": content,
                        "tool_call_id": tcid,
                    }
                )
        elif role == "reasoning":
            # EverOS 无此 role -> synthetic assistant。绝不并入 user（会污染 user owner）。
            out.append(_synthetic_assistant(f"[reasoning] {content}", agent_id, ts))
        elif role == "system":
            continue  # 配置噪声，drop
        # 真·未知 role（真库里有 gemini/info/error）：保守 skip
    return out
