# cass_corpus/pruner.py
# 确定性 transcript 清洗。接口 Pruner 可替换(以后可换 LLMLingua-2 等成熟方案,实现同协议即可)。
# 默认 DeterministicPruner 实现接地规则:
#   - 系统/developer 提示=配置非情景记忆 → 整段丢(Letta/Mem0/Generative Agents;研究 §6)
#   - user/assistant=意图/决定/推理 → 忠实保留不压(决定>观察;§3.1 §4.1)
#   - tool 调用 → 压成一行 [tool: name](§9.1)
#   - toolResult=观察/dump → 超阈值才"首尾保留+关键词采样+指针"截断,绝不盲截、不预摘要(§4.1-4.3)
# 全程确定性、无 LLM、无模型依赖(gbrain deterministic-collectors 原则)。
import json
import re
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Msg:
    idx: int
    role: str
    content: str


class Pruner(Protocol):
    def prune(self, msgs: "list[Msg]") -> "list[Msg]":
        """清洗一组消息:丢弃/截断/合并。返回保留(且 content 已变换)的消息。"""
        ...


_TOOL_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
_DEFAULT_KEYWORDS = r"ERROR|Exception|Traceback|Panic|Fatal|FAIL|WARN|assert"
_HARD_ERR = re.compile(r"ERROR|FAIL|Traceback|Exception|Panic|Fatal|assert", re.IGNORECASE)


class DeterministicPruner:
    MIN_CAP      = 200
    MAX_ERR_LINE = 300
    RESCUE_FRAC  = 4
    max_err_lines = 10          # 类默认;Task 2 的 __init__ 会设为实例属性

    def __init__(self, *, drop_roles=("developer",), tool_call_roles=("tool",),
                 observation_roles=("toolResult",), tool_result_max_chars=1500,
                 head_lines=6, tail_lines=6, max_line_chars=500, max_keyword_lines=20,
                 keyword_pattern=_DEFAULT_KEYWORDS):
        self.drop_roles = set(drop_roles)
        self.tool_call_roles = set(tool_call_roles)
        self.observation_roles = set(observation_roles)
        self.tool_result_max_chars = tool_result_max_chars
        self.head_lines = head_lines
        self.tail_lines = tail_lines
        self.max_line_chars = max_line_chars
        self.max_keyword_lines = max_keyword_lines
        self.keyword_re = re.compile(keyword_pattern, re.IGNORECASE)

    def prune(self, msgs):
        out = []
        for m in msgs:
            if m.role in self.drop_roles:
                continue                                          # 配置噪声整段丢
            if m.role in self.tool_call_roles:
                out.append(Msg(m.idx, m.role, self._collapse_tool_call(m.content)))
            elif m.role in self.observation_roles:
                out.append(Msg(m.idx, m.role, self._truncate_observation(m.content)))
            else:
                out.append(m)                                     # user/assistant 等忠实保留
        return out

    def _collapse_tool_call(self, content):
        name = None
        try:
            obj = json.loads(content)
            if isinstance(obj, dict):
                name = obj.get("name") or obj.get("tool") or obj.get("function")
        except Exception:
            mt = _TOOL_NAME_RE.search(content or "")
            name = mt.group(1) if mt else None
        return f"[tool: {name}]" if name else "[tool call]"

    def _cap(self, line):
        return line if len(line) <= self.max_line_chars else line[:self.max_line_chars] + "…"

    def _truncate_observation(self, content):
        if content is None:
            return ""
        if len(content) <= self.tool_result_max_chars:
            return content                                        # 阈值内 → 原样(忠实)
        lines = content.splitlines()
        n = len(lines)
        if n <= self.head_lines + self.tail_lines:
            body = "\n".join(self._cap(l) for l in lines)         # 行少(或单行巨型)→ 仅按字符封顶
        else:
            head = [self._cap(l) for l in lines[:self.head_lines]]
            tail = [self._cap(l) for l in lines[n - self.tail_lines:]]
            middle = lines[self.head_lines:n - self.tail_lines]
            kw = [self._cap(l) for l in middle if self.keyword_re.search(l)][:self.max_keyword_lines]
            parts = list(head)
            if kw:
                parts += ["…〔关键行〕"] + kw
            parts += [f"…〔截断 {len(middle) - len(kw)} 行〕"] + tail
            body = "\n".join(parts)
        return f"{body}\n〔原始工具输出已截断:{n} 行 / {len(content)} 字符;完整内容见 CASS 原会话〕"

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
        rescue_budget = (cap // self.RESCUE_FRAC) if rescue_errors else 0
        # 第一遍:head/tail 按 full cap 划,从中段抢救硬错误行(≤ rescue_budget)
        head = content[: cap * 2 // 3]
        tail = content[-(cap - len(head)):]
        mid  = content[len(head): len(content) - len(tail)]
        errs, used = [], 0
        if rescue_errors:
            for l in mid.splitlines():
                m = _HARD_ERR.search(l)
                if not m: continue
                l = self._cap_line(l, m.start())                      # 以关键词位置截窗(plan R2 P2)
                if len(errs) >= self.max_err_lines: break              # 行数到顶 → 停
                if used + len(l) > rescue_budget: continue             # 太大塞不下 → 跳过找短的(codex plan R0 P1)
                errs.append(l); used += len(l)
        # 第二遍:有抢救则 head/tail 收缩 used(变小=子集,rescued 行仍在更大 mid 里、绝不重复;codex R3 回流 + plan R1 P2 去重)→ 总量 = cap
        if used:
            body = cap - used
            head = content[: body * 2 // 3]
            tail = content[-(body - len(head)):] if body - len(head) > 0 else ""
        cut  = len(content) - len(head) - len(tail)
        parts = [head]
        if errs: parts.append("…〔硬错误行〕\n" + "\n".join(errs))
        parts.append(f"…〔截断 {cut} 字符;完整见 CASS 原会话〕")
        parts.append(tail)
        return "\n".join(parts)
