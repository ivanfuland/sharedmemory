# distill/filters.py
# 已发现 agent slug（contracts/cass-canonical.md line 35）；新组合走 quarantine
KNOWN_SOURCES = {
    "claude_code", "codex", "gemini", "pi_agent",
    "openclaw/alice", "openclaw/clawra", "openclaw/javich",
    "openclaw/justin", "openclaw/main", "openclaw/wood",
}
# 桥自身 / dreaming 自产 / cron transcript → 防自噬跳过（spec §2.6 line 153）
SELF_SOURCES = {"distill-bridge", "dreaming", "cron"}
# 纯噪声字符串兜底白名单（spec §2.6 line 154）
NOISE_WHITELIST = ("HEARTBEAT_OK", "HEARTBEAT_CHECK", "NO_REPLY", "boot-check")


def classify_source(agent, workspace):
    a = (agent or "").strip()
    if a in SELF_SOURCES or a == "dreaming" or a.startswith("dreaming/") or a == "cron" or a.startswith("cron/"):
        return "skip_self"
    if a in KNOWN_SOURCES:
        return "distill"
    return "quarantine_unknown"


def is_noise(content):
    return (content or "").strip() in NOISE_WHITELIST


def filter_span_messages(rows):
    kept = [r for r in rows if not is_noise(r.get("content", ""))]
    return kept, len(rows) - len(kept)
