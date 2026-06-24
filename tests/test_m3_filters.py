from distill import filters

def test_self_ingestion_blocked():
    # 桥自身 source / dreaming 自产 → skip_self（防自噬）
    assert filters.classify_source("openclaw/main", "/x") == "distill"
    assert filters.classify_source("distill-bridge", "/x") == "skip_self"
    assert filters.classify_source("dreaming", "/x") == "skip_self"

def test_precise_self_source_match():
    # precise self-detection, R3-review IMP-2: cronos/dreamingfoo must not self-skip
    assert filters.classify_source("cronos", "/x") == "quarantine_unknown"
    assert filters.classify_source("dreamingfoo", "/x") == "quarantine_unknown"
    # but cron/x and dreaming/x still self-skip
    assert filters.classify_source("cron/daily", "/x") == "skip_self"
    assert filters.classify_source("dreaming/session", "/x") == "skip_self"

def test_unknown_source_quarantined():
    assert filters.classify_source("brand_new_agent", "/x") == "quarantine_unknown"

def test_known_agent_distilled():
    assert filters.classify_source("claude_code", "/home/ivan/projects/foo") == "distill"
    assert filters.classify_source("codex", "/x") == "distill"

def test_noise_whitelist():
    assert filters.is_noise("HEARTBEAT_OK")
    assert filters.is_noise("  NO_REPLY \n")
    assert not filters.is_noise("我们决定用方案 X")

def test_filter_span_drops_noise():
    rows = [{"role":"user","content":"我们决定用 X","agent":"claude_code","workspace":"/x"},
            {"role":"assistant","content":"HEARTBEAT_OK","agent":"claude_code","workspace":"/x"}]
    kept, dropped = filters.filter_span_messages(rows)
    assert len(kept) == 1 and dropped == 1
