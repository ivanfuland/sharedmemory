# distill/stale.py
from distill import distiller

CONTRADICTS_FLAG = "[flag: contradicts-truth]"
_STUB_MARK = "（蒸馏桥自动创建"   # writer.page_markdown 的 stub body 标记
_JUDGE_SCHEMA = {"name": "contradiction_judge", "strict": True,
    "schema": {"type": "object", "additionalProperties": False, "required": ["contradicts"],
               "properties": {"contradicts": {"type": "boolean"}}}}

def is_high_impact(slug):
    return slug.startswith("decisions/") or slug.startswith("preferences/")

def _body_after_frontmatter(md):
    if md.startswith("---"):
        parts = md.split("---", 2)
        return parts[2].strip() if len(parts) == 3 else md.strip()
    return md.strip()

def _has_compiled_truth(md):
    body = _body_after_frontmatter(md or "")
    # 去掉标题行 + stub 标记 + alias-mirror 行后仍有实质内容 = 有 compiled truth（R3 P1-2）
    lines = [l for l in body.splitlines() if l.strip() and not l.startswith("#")
             and _STUB_MARK not in l and "alias-mirror" not in l]
    return bool(lines)

def assess_contradiction(cfg, token, slug, fact_text, call, chat=None, page_md=None):
    if not cfg.get("contradiction_check", True):
        return False
    if page_md is not None:
        md = page_md
    else:
        page = call(cfg, token, "get_page", {"slug": slug})
        md = page.get("text", "") if isinstance(page, dict) else str(page)
    if not _has_compiled_truth(md):
        return False
    chat = chat or distiller._chat_http
    body = {"model": cfg["distill"]["model"], "temperature": 0,
            "response_format": {"type": "json_schema", "json_schema": _JUDGE_SCHEMA},
            "messages": [
                {"role": "system", "content": "判断新事实是否与既有结论矛盾，只输出 {\"contradicts\": bool}。"},
                {"role": "user", "content": f"既有结论：\n{_body_after_frontmatter(md)}\n\n新事实：\n{fact_text}"}]}
    nbytes = len(_body_after_frontmatter(md).encode()) + len(fact_text.encode())
    try:
        out = chat(body, cfg)
    except Exception as e:
        distiller.audit_append(session_ref=slug, bytes_out=nbytes, model=cfg["distill"]["model"],
                               path=cfg["paths"].get("audit_log"), purpose="contradiction_judge",
                               status=f"error:{type(e).__name__}")        # 失败也审计（codex R2 P1-2）
        raise
    distiller.audit_append(session_ref=slug, bytes_out=nbytes, model=cfg["distill"]["model"],
                           path=cfg["paths"].get("audit_log"), purpose="contradiction_judge", status="ok")
    return bool(out.get("contradicts"))
