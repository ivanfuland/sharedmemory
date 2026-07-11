# cass_corpus/redact.py
# 结构化密钥脱敏：CASS→gbrain 导出边界的 defense-in-depth。
# 移植自 agentmemory src/functions/privacy.ts 的 SECRET_PATTERN_SOURCES（14 条）。
# #1 相对上游只加 bearer 关键词，敏感词须紧邻 =/:（无前导贪婪通配 → 线性、不误杀 tokenizer/author）。
import re

_REDACTED = "[REDACTED_SECRET]"

_SECRET_PATTERNS = [
    # 1. 关键词赋值（含 bearer）：敏感词紧邻 =/: 才命中
    re.compile(r'(?:api[_-]?key|secret|token|password|credential|auth|bearer)[\s]*[=:]\s*["\']?[A-Za-z0-9_\-/.+]{20,}["\']?', re.IGNORECASE),
    # 2. Bearer header
    re.compile(r'Bearer\s+[A-Za-z0-9._\-+/=]{20,}', re.IGNORECASE),
    # 3. OpenAI project key
    re.compile(r'sk-proj-[A-Za-z0-9\-_]{20,}'),
    # 4. 通用 sk/pk/rk/ak 前缀
    re.compile(r'(?:sk|pk|rk|ak)-[A-Za-z0-9][A-Za-z0-9\-_]{19,}'),
    # 5. Anthropic key
    re.compile(r'sk-ant-[A-Za-z0-9\-_]{20,}'),
    # 6. GitHub token
    re.compile(r'gh[pus]_[A-Za-z0-9]{36,}'),
    # 7. GitHub fine-grained PAT
    re.compile(r'github_pat_[A-Za-z0-9_]{22,}'),
    # 8. Slack bot token
    re.compile(r'xoxb-[A-Za-z0-9\-]+'),
    # 9. AWS access key id
    re.compile(r'AKIA[0-9A-Z]{16}'),
    # 10. Google API key
    re.compile(r'AIza[A-Za-z0-9\-_]{35}'),
    # 11. JWT
    re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'),
    # 12. npm token
    re.compile(r'npm_[A-Za-z0-9]{36}'),
    # 13. GitLab PAT
    re.compile(r'glpat-[A-Za-z0-9\-_]{20,}'),
    # 14. DigitalOcean token
    re.compile(r'dop_v1_[A-Za-z0-9]{64}'),
]


def redact_secrets(text: str) -> str:
    """把结构化密钥替换成 [REDACTED_SECRET]。纯正则、零依赖、幂等。空/None 原样返回。"""
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub(_REDACTED, text)
    return text


_IDENTITY_PROTECT = {"external_id", "source_id", "session_key", "agent", "source", "date"}
_FM_KEY = re.compile(r"^([A-Za-z_]+):\s")


def redact_transcript(text: str) -> str:
    """脱敏 transcript：保护 frontmatter 身份行不被改写（否则守卫误 raise / backfill orphan）。
    - 无 frontmatter → 全文 redact_secrets。
    - 未闭合 frontmatter → 原样返回（交 _validate_text_identity 兜底拒写，不落盘）。
    - 正常：身份行逐字保留；title/workspace + 正文照常脱敏。"""
    if not text.startswith("---\n"):
        return redact_secrets(text)
    end = text.find("\n---\n", 4)
    if end == -1:
        return text                                  # 未闭合 → 不动身份区
    fm, rest = text[:end], text[end:]                # rest 以闭合 \n---\n 起（含正文）
    out = []
    for line in fm.split("\n"):
        m = _FM_KEY.match(line)
        if m and m.group(1) in _IDENTITY_PROTECT:
            out.append(line)                         # 身份行逐字
        else:
            out.append(redact_secrets(line))
    return "\n".join(out) + redact_secrets(rest)
