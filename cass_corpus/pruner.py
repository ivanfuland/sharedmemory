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
    tool_call_id: Optional[str] = None   # 配对 id(reader 从 extra_bin msgpack 读,回退 extra_json)
    unpaired: bool = False               # tool_result 配不上本会话任何 call(reader 推导,非 extra 字段)


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

    def __init__(self, *, tool_call_cap=1200, tool_result_cap=2500, reasoning_cap=1000,
                 max_err_lines=10, warn=None):
        # cap 已按生产库真实分布量定(PR-C,2026-07-11;spec §U1 收口)。只读量 73k tool 消息:
        #   tool_call  p50=217 p90=1053 → cap=1200(截断 8.5%,喂 25MB/完整 38MB);
        #   tool_result p50=689 p90=9698 极重尾 → cap=2500(截断 31.5%,喂 81MB/完整 232MB);
        #     _clamp 有损但保 head+tail+抢救硬错误行(verdict/报错常在 tail);实测 ~2.6% 会丢末行。
        #   reasoning_cap 保留 1000(实际 moot):claude reasoning content 空(2万条)、codex reasoning
        #     主体是 extra_bin 里的 Fernet 加密(gAAAAA... 客户端解不了),reader 只喂 content 列;
        #     仅 202 条 codex reasoning 有明文 content 且全 ≤602 字符 → 永不触 cap。openclaw 无 reasoning role。
        #     待将来出现长明文 reasoning(如 openclaw thinking 入库)再按分布量定。
        # ⚠ 本次分布只量当前 reader 的 content 文本视图;extra_bin 另有结构化/多模态 payload
        #   (image_url / toolUseResult / thinkingSignature)reader 未组装 → 若将来 reader 改组装,须重测。
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

    def _cap_line(self, l, at=0, max_len=None):
        # 以 at(关键词位置)为中心取窗,总长 ≤ max_len(默认 MAX_ERR_LINE);预算紧时可传更小的窗
        n = max_len if max_len is not None else self.MAX_ERR_LINE
        if len(l) <= n: return l
        win = n - 2                                     # 留 2 给两端省略号
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
        errs, used, pos, skipped = [], 0, 0, []
        if rescue_errors:
            for l in content.split("\n"):                       # 逐行:短行整条抢救(便宜)、巨型行关键词居中截窗
                line_start = pos; pos += len(l) + 1             # +1 = split 丢弃的 "\n"
                rescue_at = None
                for m in _HARD_ERR.finditer(l):                 # 查该行所有 ERROR(不止第一个,codex PR R3 P1)
                    if line_start + m.end() <= head_end or line_start + m.start() >= tail_start:
                        continue                                 # 该关键词完整可见于 head/tail
                    rescue_at = m.start(); break                 # 第一个落被丢弃区/横跨边界的关键词
                if rescue_at is None: continue                   # 全部可见 或 无 ERROR → 不抢救(无重复,codex PR R2 P1)
                seg = self._cap_line(l, rescue_at)               # 短行原样、巨型行关键词居中截窗(codex PR R1 P1)
                cost = len(seg) + 1                              # +1 计入 join 换行(严格守恒,codex PR R0 P2)
                if len(errs) >= self.max_err_lines: break
                if used + cost > rescue_budget:
                    skipped.append((l, rescue_at)); continue     # 塞不下 → 记下,末尾用剩余预算补救(先给更短的机会)
                errs.append(seg); used += cost
            # 末尾:budget 有剩余且有被跳过的 ERROR → 用剩余预算截一个窗口,保证有预算就不静默丢(codex PR R4 P1)
            room = rescue_budget - used
            if skipped and len(errs) < self.max_err_lines and room > 20:
                l, at = skipped[0]
                errs.append(self._cap_line(l, at, room - 1))     # 窗口 ≤ 剩余预算
        cut  = len(content) - len(head) - len(tail)
        parts = [head]
        if errs: parts.append("…〔硬错误行〕\n" + "\n".join(errs))
        parts.append(f"…〔截断 {cut} 字符;完整见 CASS 原会话〕")
        parts.append(tail)
        return "\n".join(parts)
