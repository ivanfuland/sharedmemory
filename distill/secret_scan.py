import re
VERSION = "1"   # 进 benchmark fingerprint：扫描规则变更即令旧 gold 失配重建
_HARD={"api_key":re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),"bearer":re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]{12,}"),
 "aws":re.compile(r"AKIA[0-9A-Z]{16}"),"privkey":re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
 "jwt":re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}")}
# 上下文感知 hex：仅当长 hex 紧邻 credential 关键词才算（git SHA/md5 示例不误杀）
_CTX_HEX=re.compile(r"(?i)(api[_\-]?key|token|secret|password|passwd|credential|auth)\W{0,4}[:=]\W{0,4}[0-9a-f]{32,}")
def scan_span(span):
    rows=span.get("span", span if isinstance(span,list) else [])
    text="\n".join(r.get("content","") or "" for r in rows)
    hits={n for n,rx in _HARD.items() if rx.search(text)}
    if _CTX_HEX.search(text): hits.add("ctx_hex_secret")
    return sorted(hits)
