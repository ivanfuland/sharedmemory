# cass_corpus/render.py
# 渲染清洗后的消息 → gbrain session_corpus 的 transcript 文件(.md)。
# 关键约束:
#   - frontmatter / 文件名 必须确定性(同会话内容不变 → content_hash 不变 → gbrain 自动去重)。
#     绝不放导出时间戳。
#   - 绝不带 dream_generated / mode:lsd(否则触发 gbrain self-consumption guard 被跳过)。
import hashlib
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


# 稳定会话身份 = CASS canonical 的唯一约束 UNIQUE(source_id, agent_id, external_id)。
# ⚠ 绝不能用 conversation_id:它是 SQLite 的 rowid,全量重摄重建库即重新发号
#   (2026-07-09 6-role 换库实测:2361 个会话里 2290 个变号,71 个纯属巧合没变)。
#   文件名一变,gbrain 就把同一会话当新文档 → 全量重炼 + 留下孤儿页。
# ⚠ 也不能只哈希 external_id:唯一约束带 source_id 和 agent。
_KEY_BYTES = 8          # 64 bit;2424 会话零碰撞,10 万会话碰撞概率 ~3e-10
# 前缀 's' 保证 key 永不为纯十进制串。裸 16 hex 有 (10/16)^16 ≈ 2.8e-4 概率全是数字
# (实测真库 2424 条里就有 1 条:`...-3845786846798581.md`),会被下游按 rowid 的
# `/-(\d+)\.md$/` 误捕(codex PR#41 审出)。
_KEY_PREFIX = "s"


def session_key(meta):
    """跨重摄稳定的会话摘要(`s` + 16 hex)。无 external_id(老/合成 schema)→ 回退到 rowid 派生,
    仍确定性但**不稳定**——如实反映"这个库没给出稳定身份",不假装有。"""
    ext = meta.get("external_id")
    if ext:
        raw = f"{meta.get('source_id') or ''}\x00{meta.get('agent') or ''}\x00{ext}"
    else:
        raw = f"__legacy_rowid__\x00{meta.get('agent') or ''}\x00{meta['id']}"
    return _KEY_PREFIX + hashlib.blake2b(raw.encode("utf-8"), digest_size=_KEY_BYTES).hexdigest()


def transcript_filename(meta):
    """确定性且跨重摄稳定的文件名:<date>-cass-<agent>-<session_key>.md。
    date/agent 只为人眼可读;身份由 session_key 承担。"""
    return f"{_date(meta.get('started_at'))}-cass-{_safe(meta.get('agent'))}-{session_key(meta)}.md"


# 覆盖 str.splitlines() 认作行边界的全部分隔符：LF CR VT FF FS-RS NEL LS PS。
# parser（export._parse_frontmatter_identity）用 splitlines() 切行，_clean 必须清同一集合，
# 否则 U+2028 等分隔符能绕过净化、在 splitlines 下拆出伪 frontmatter 行（codex 实现审 R1 P2#2）。
_FM_BREAK = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


def _clean(v):
    """净化拼入 frontmatter 的值：任何行分隔符 → 空格，防止值内分隔符注入伪 frontmatter 行（codex R1 P1-3 + R2 P2#2）。"""
    return _FM_BREAK.sub(" ", v or "")


def _frontmatter(meta):
    title = _clean(meta.get("title")).strip()
    # ⚠ 绝不写 conversation_id(rowid):它重摄即变 → frontmatter 变 → content_hash 变 →
    #   gbrain 仍把同一会话当新内容全量重炼。文件名稳定但正文漂移 = 修了一半等于没修
    #   (codex PR#41 审出的 P1:实测同一会话 rowid 72→116,文件名不变但渲染 hash 变)。
    #   要对当前库调试,用 external_id 反查:SELECT id FROM conversations WHERE external_id=?
    # agent 是 CASS DB 的 slug（安全派生值），不经 _clean；万一含换行也会被
    # export._validate_text_identity 的 dup/身份不符检查 fail-loud 拦下，不会静默污染。
    lines = ["---", "source: cass", f"session_key: {session_key(meta)}",
             f"agent: {meta.get('agent', '')}", f"date: {_date(meta.get('started_at'))}"]
    if meta.get("external_id"):
        lines.append(f"external_id: {_clean(meta['external_id'])}")
    if meta.get("source_id"):
        lines.append(f"source_id: {_clean(meta['source_id'])}")
    if meta.get("workspace"):
        lines.append(f"workspace: {_clean(meta['workspace'])}")
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
