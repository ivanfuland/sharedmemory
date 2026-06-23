import json, os
from datetime import datetime, timezone

def _default_path():
    return os.environ.get("DISTILL_AUDIT") or os.path.join(os.path.dirname(__file__), "audit.log")

def audit_append(*, session_ref, bytes_out, model, path=None):
    """R5：每次原文出机记一行。path 每次调用解析（便于测试注入）。"""
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "session_ref": session_ref, "bytes_out": bytes_out, "model": model}
    with open(path or _default_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
