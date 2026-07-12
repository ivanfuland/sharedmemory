"""CASS canonical row -> 归一化 6-role dict。

⚠️ 配对 id 在 `extra_bin` 的 MessagePack **顶层**，不在 `extra_json`。
CASS `sqlite.rs:12842` 把非空的 `msg.extra_json` 整个打包进 `extra_bin`；空对象则往
`extra_json` 列写字面 `'{}'`。两列严格二选一，`extra_json='{}'` 是「本条无附加信息」的
空壳记号。读 `extra_json.tool_call_id` 会得到 100% 空值**且不报错**。

msgpack 顶层（三源一致，实测）：
    tool_call    {timestamp, type, payload, tool_call_id, tool_call_args}
    tool_result  {timestamp, type, payload, tool_call_id}
`payload` 是原始源 blob，各源结构不同（claude/openclaw 是原始 jsonl 行、codex 是嵌套），
**不可依赖**。工具名从 `content` 前缀解析（三源统一 `<name>({json})`）。
"""

from __future__ import annotations

import json
import re

# ⚠️ 用本分支新建的公开名 extra_dict（内部即 _extra_dict）。绝不复制它的实现 ——
# 它的降级策略比我们自写的更细（_DECODE_ERRORS 白名单让 MemoryError/RecursionError loud fail）。
# 注意 extra_dict **只解包、不做类型收紧**：非 str 的 tool_call_id 丢弃是 read_messages
# (reader.py:148-151) 的职责，本模块在下面 read_message 里自己 isinstance 判。
from cass_corpus.reader import extra_dict

_TOOL_NAME = re.compile(r"^([A-Za-z_]\w*)\(")


def parse_tool_name(content: str) -> str:
    m = _TOOL_NAME.match(content or "")
    return m.group(1) if m else ""


def args_to_json_str(v) -> str:
    """`tool_call_args` 是三形态：全库实测 dict 66284 / str 477 / 无 None。

    - dict（绝大多数）：EverOS `ToolFunctionDTO.arguments` 要 `str` 且应是合法 JSON，
      **绝不能用 `str(dict)`** —— 那是 Python repr（单引号），DTO 不报错但提炼器读到非法 JSON。
    - str（477 条）：codex `apply_patch` 的非 JSON 补丁文本（serde 解析失败原样返回 str）。
      **原样透传**（补丁原文对提炼器比伪 JSON 更有用；DTO 只硬要求 str）—— 故 str 分支是
      **必要覆盖，不是防御性**。M0 的 `role_map._args_str` 的 str-原样本来就对（早期 M1a 草案
      基于「已是 str」误删过它，实则 dict 与 str 两种形态都真实存在，采样 200 抽不中那 477 条）。
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v                                    # 477 条 apply_patch 补丁原文，原样透传（必要，非防御）
    return json.dumps(v, ensure_ascii=False)


def read_message(row, extra_cols: list[str]) -> dict:
    """CASS row -> 归一化 dict。extra 解析委托 `cass_corpus.reader.extra_dict`。

    注意 `extra_dict` 对空壳 `extra_json='{}'` 返回 `{}`（不是 `None`），坏数据返回 `None`。
    两者对下游等价（都拿不到 id），故统一 `or {}`。`extra_dict` 只解包不做类型收紧，
    故非 str 的 tool_call_id 由本函数下方自己 isinstance 判掉。
    """
    role = row["role"] or ""
    content = row["content"] or ""
    ex = extra_dict(row, extra_cols) or {}

    # ⚠️ 绝不在此回落 idx+1（codex R0 P0#3）：created_at 是 ms epoch（1751961600300），
    # idx+1 是小整数（1,2,3）。同一会话混用两种量纲后，batching 的
    # `want = max(ts, last+1)` 会把小值抬到十七亿，total_skew 瞬间超 max_skew_ms
    # -> 整条会话被误 quarantine。
    # 缺失的 ts 留 0，由 `batching.ensure_unique_timestamps` 统一顺延填补（不计 skew）。
    ts = int(row["created_at"] or 0)

    tcid = ex.get("tool_call_id")
    return {
        "role": role,
        "content": content,
        "timestamp": ts,
        # 非 str 的 id（bytes/int）宁可当没有 —— 否则会渲染成 "b'abc'" 这样的垃圾
        "tool_call_id": tcid if isinstance(tcid, str) else "",
        "tool_call_args": args_to_json_str(ex.get("tool_call_args")),
        "tool_name": parse_tool_name(content) if role == "tool_call" else "",
    }


def read_conversation(rows, extra_cols: list[str]) -> list[dict]:
    return [read_message(r, extra_cols) for r in rows]
