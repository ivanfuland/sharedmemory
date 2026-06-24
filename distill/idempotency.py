# distill/idempotency.py
import hashlib, re

_KIND_DIR = {"person": "people", "project": "projects",
             "decision": "decisions", "preference": "preferences"}

def normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip())

def fact_key(source_ref, entity_slug, entry_type, fact_text):
    digest = hashlib.sha256(normalize(fact_text).encode()).hexdigest()
    raw = f"{source_ref}|{entity_slug}|{entry_type}|{digest}"
    return hashlib.sha256(raw.encode()).hexdigest()

def key_marker(key):
    return f"[dk:{key[:16]}]"

def slug_for(entity_kind, entity_name):
    d = _KIND_DIR.get(entity_kind, "projects")
    # gbrain put_page 要求 slug 小写（大写字母 → "Page not found"，e2e 实测）；
    # name 内 "/" 会破坏 dir/name 路径分段 → 替成 "-"。中文不受 .lower() 影响。
    name = normalize(entity_name).lower().replace("/", "-")
    return f"{d}/{name}"
