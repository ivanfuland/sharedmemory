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
