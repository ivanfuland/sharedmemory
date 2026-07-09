# cass_corpus/render.py
# 渲染清洗后的消息 → gbrain session_corpus 的 transcript 文件(.md)。
# 关键约束:
#   - frontmatter / 文件名 必须确定性(同会话内容不变 → content_hash 不变 → gbrain 自动去重)。
#     绝不放导出时间戳。
#   - 绝不带 dream_generated / mode:lsd(否则触发 gbrain self-consumption guard 被跳过)。
import re
from datetime import datetime, timedelta, timezone

_TZ = timezone(timedelta(hours=8))   # GMT+8:Ivan "哪天聊的" 语义,确定性
_ROLE_LABEL = {"user": "User", "agent": "Assistant", "assistant": "Assistant",
               "tool_call": "Tool Call", "tool_result": "Tool Result",
               "toolResult": "Tool Result", "tool": "Tool Result",
               "reasoning": "Reasoning", "system": "System"}
_SLUG_SAFE = re.compile(r"[^a-z0-9]+")


def _date(epoch):
    if not epoch:
        return "0000-00-00"
    e = int(epoch)
    if e > 1_000_000_000_000:        # CASS 时间戳为毫秒 → 转秒
        e //= 1000
    return datetime.fromtimestamp(e, _TZ).date().isoformat()


def _safe(s):
    return _SLUG_SAFE.sub("-", (s or "").lower()).strip("-") or "x"


def transcript_filename(meta):
    """确定性文件名:<date>-cass-<agent>-<convid>.md(convid 稳定 → 同会话同名)。"""
    return f"{_date(meta.get('started_at'))}-cass-{_safe(meta.get('agent'))}-{meta['id']}.md"


def _frontmatter(meta):
    title = (meta.get("title") or "").replace("\n", " ").strip()
    lines = ["---", "source: cass", f"conversation_id: {meta['id']}",
             f"agent: {meta.get('agent', '')}", f"date: {_date(meta.get('started_at'))}"]
    if meta.get("workspace"):
        lines.append(f"workspace: {meta['workspace']}")
    if title:
        lines.append(f"title: {title}")
    lines.append("---")
    return "\n".join(lines)


def _marker(m):
    """配对标记:有 id → [#id];tool_result 无 id 或 unpaired → [unpaired](契约 P-原则-3)。"""
    is_call = m.role == "tool_call"
    is_res  = m.role in ("tool_result", "toolResult", "tool")
    if not (is_call or is_res):
        return ""
    if m.unpaired:
        return " [unpaired]"
    if m.tool_call_id:
        return f" [#{m.tool_call_id}]"
    return " [unpaired]" if is_res else ""      # 结果无 id → unpaired;call 无 id 不标


def render(meta, pruned_msgs):
    """meta(reader 元数据) + pruned_msgs(Pruner 输出)→ 完整 transcript 文本。"""
    parts = [_frontmatter(meta), ""]
    for m in pruned_msgs:
        label = _ROLE_LABEL.get(m.role, m.role)
        content = (m.content or "").strip()
        if not content:
            continue
        parts.append(f"### {label}{_marker(m)}")
        parts.append(content)
        parts.append("")          # turn 间空行(对齐 gbrain chunker 边界)
    return "\n".join(parts).rstrip() + "\n"
