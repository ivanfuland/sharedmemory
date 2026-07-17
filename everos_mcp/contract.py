# everos_mcp/contract.py
"""入参契约门 + 确定性截断算法。

顺序冻结(R7 教训——尾随换行若先 strip 会被洗掉,必须先查原始输入):
  1. 在 RAW 输入上查 str.splitlines() 承认的全部行边界字符
  2. strip 首尾空白
  3. 空字符串判 error
  4. 按 Unicode code point 计数,超 150 判 error
"""

_WHITELIST = {"agent_case": ("task_intent","approach","key_insight"),
              "agent_skill": ("name","description","content")}
_LINEBREAKS = tuple(chr(c) for c in (0x0A,0x0B,0x0C,0x0D,0x1C,0x1D,0x1E,0x85,0x2028,0x2029))  # str.splitlines() 文档全集,含 FS/GS/RS

ERROR_CODES = frozenset({
    "task_has_linebreak",
    "task_empty",
    "task_too_long",
    "limit_out_of_range",
    "everos_timeout",
    "everos_http_error",
    "everos_bad_response",
    "ledger_unavailable",
    "ledger_timeout",
    "review_overdue",
    "internal",
})


class ContractError(Exception):
    def __init__(self, code, msg=""):
        self.code = code; super().__init__(msg or code)


def validate_task(raw: str) -> str:
    if any(ch in raw for ch in _LINEBREAKS):
        raise ContractError("task_has_linebreak")
    task = raw.strip()
    if not task:
        raise ContractError("task_empty")
    if len(task) > 150:
        raise ContractError("task_too_long")
    return task


def validate_limit(v: int) -> int:
    if not (1 <= v <= 5):
        raise ContractError("limit_out_of_range")
    return v


def clamp_payload(payload: dict, mem_type: str, cap: int = 8000):
    fields = _WHITELIST[mem_type]
    out = {f: payload.get(f) for f in fields}
    lens = {f: len(out[f]) if isinstance(out[f], str) else 0 for f in fields}
    truncated = False
    while sum(lens.values()) > cap:
        # 最长字段;等长 tie 取白名单顺序靠前者
        longest = max(fields, key=lambda f: (lens[f], -fields.index(f)))
        cut = min(lens[longest], sum(lens.values()) - cap)
        out[longest] = out[longest][: lens[longest] - cut]
        lens[longest] -= cut
        truncated = True
    return out, truncated
