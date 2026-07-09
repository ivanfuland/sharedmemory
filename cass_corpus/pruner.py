# cass_corpus/pruner.py
# 确定性 transcript 清洗。接口 Pruner 可替换(以后可换 LLMLingua-2 等成熟方案,实现同协议即可)。
# 默认 DeterministicPruner 实现接地规则:
#   - 系统/developer 提示=配置非情景记忆 → 整段丢(Letta/Mem0/Generative Agents;研究 §6)
#   - user/assistant=意图/决定/推理 → 忠实保留不压(决定>观察;§3.1 §4.1)
#   - tool_call/tool_result → 超阈值才"首尾保留+关键词采样+指针"截断,绝不盲截、不预摘要(§4.1-4.3)
# 全程确定性、无 LLM、无模型依赖(gbrain deterministic-collectors 原则)。
import re
import sys
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class Msg:
    idx: int
    role: str
    content: str
    tool_call_id: Optional[str] = None   # 配对标记(reader 从 extra_json 读)
    unpaired: bool = False               # tool_result 无关联 call(契约 extra_json.unpaired)


class Pruner(Protocol):
    def prune(self, msgs: "list[Msg]") -> "list[Msg]": ...


_HARD_ERR = re.compile(r"ERROR|FAIL|Traceback|Exception|Panic|Fatal|assert", re.IGNORECASE)

# 语义分类:6-role + legacy 防御映射(迁移残留/smoke 跑未迁移库)
_DROP = {"system", "developer", "error", "info"}          # 配置/事件噪声
_KEEP = {"user", "assistant", "agent", "gemini"}          # 语义内容忠实留
# role → (用哪个 cap 属性, 是否抢救硬错误)
_CLAMP = {
    "tool_call":   ("tool_call_cap",   True),
    "tool_result": ("tool_result_cap", True),
    "reasoning":   ("reasoning_cap",   False),            # 关抢救(散文里 assert/fail 非报错)
    "tool":        ("tool_result_cap", True),             # legacy:工具输出,绝不 collapse
    "toolResult":  ("tool_result_cap", True),             # legacy
}


class DeterministicPruner:
    MIN_CAP        = 200
    MAX_ERR_LINE   = 300
    RESCUE_FRAC    = 4
    MARKER_RESERVE = 48     # 给指针/标记/join 换行预留额度,保证总输出 ≤ cap(codex PR R1 P2)

    def __init__(self, *, tool_call_cap=800, tool_result_cap=1500, reasoning_cap=1000,
                 max_err_lines=10, warn=None):
        # cap 是 provisional 默认(spec §7.1/§U1:真值待 franken 产真 role 后按分布量定)
        self.tool_call_cap   = tool_call_cap
        self.tool_result_cap = tool_result_cap
        self.reasoning_cap   = reasoning_cap
        self.max_err_lines   = max_err_lines
        self._warn = warn or (lambda m: print(m, file=sys.stderr))   # 默认 loud(stderr),非 no-op

    def prune(self, msgs):
        out = []
        for m in msgs:
            if m.role in _DROP:
                continue
            if m.role in _KEEP:
                out.append(m)
            elif m.role in _CLAMP:
                cap_attr, rescue = _CLAMP[m.role]
                out.append(Msg(m.idx, m.role,
                               self._clamp(m.content, getattr(self, cap_attr), rescue_errors=rescue),
                               m.tool_call_id, m.unpaired))
            else:
                self._warn(f"[pruner] unknown role kept: {m.role!r} (idx={m.idx})")  # 不 fail-open 静默
                out.append(m)
        return out

    def _cap_line(self, l, at=0):
        # 超长行以 at(关键词位置)为中心取窗,总长 ≤ MAX_ERR_LINE(防越界 + 深埋 ERROR 被截没,codex R3 + plan R2 P2)
        if len(l) <= self.MAX_ERR_LINE: return l
        win = self.MAX_ERR_LINE - 2                      # 留 2 给两端省略号
        start = max(0, min(at - win // 2, len(l) - win))
        return ("…" if start > 0 else "") + l[start:start + win] + ("…" if start + win < len(l) else "")

    def _clamp(self, content, cap, rescue_errors=True):
        if content is None:      return ""
        cap = max(cap, self.MIN_CAP)                 # 下界:cap 过小/0 不退化成整段
        if len(content) <= cap:  return content      # 阈值内 → 原样(忠实)
        avail = cap - self.MARKER_RESERVE            # 预留 marker 额度 → 总输出 ≤ cap(codex PR R1 P2)
        rescue_budget = (avail // self.RESCUE_FRAC) if rescue_errors else 0
        # 预留 rescue_budget:head/tail 固定占 avail - rescue_budget(不回流、不收缩)。
        # 抢救只从被丢弃的 mid 扫(与 head/tail 不相交)→ 无重复、无 shrink 夹缝丢错误(codex PR R0 P1)。
        body = avail - rescue_budget
        head = content[: body * 2 // 3]
        tail = content[-(body - len(head)):] if body - len(head) > 0 else ""
        # 行边界 snap:head 缩到最后一个换行、tail 扩到最前一个换行,避免把一行(及其 ERROR 关键词)从中劈开(codex PR R1 P1)
        nl = head.rfind("\n")
        if nl != -1: head = head[:nl + 1]
        if tail:
            nl = tail.find("\n")
            if nl != -1: tail = tail[nl:]
        head_end, tail_start = len(head), len(content) - len(tail)
        errs, used, pos = [], 0, 0
        if rescue_errors:
            for l in content.split("\n"):                            # 扫原文整行(不切片)→ 关键词/整行绝不被劈开(codex PR R1 P1)
                start, end = pos, pos + len(l); pos = end + 1
                if end <= head_end or start >= tail_start:
                    continue                                          # 整行已在 head/tail 里(不重复、不丢)
                m = _HARD_ERR.search(l)
                if not m: continue
                l = self._cap_line(l, m.start())                      # 以关键词位置截窗
                cost = len(l) + 1                                     # +1 计入 join 换行(严格守恒,codex PR R0 P2)
                if len(errs) >= self.max_err_lines: break             # 行数到顶 → 停
                if used + cost > rescue_budget: continue              # 塞不下 → 跳过找短的
                errs.append(l); used += cost
        cut  = len(content) - len(head) - len(tail)
        parts = [head]
        if errs: parts.append("…〔硬错误行〕\n" + "\n".join(errs))
        parts.append(f"…〔截断 {cut} 字符;完整见 CASS 原会话〕")
        parts.append(tail)
        return "\n".join(parts)
