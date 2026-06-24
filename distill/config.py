# distill/config.py
import os, json

def _req(k):
    v = os.environ.get(k)
    assert v, f"missing env {k} (蒸馏走 API key，禁订阅 OAuth — spec §12.1)"
    return v

def load(bench_path="config/m1-benchmarks.json", bridge_path="config/m3-bridge.json"):
    with open(bench_path, encoding="utf-8") as f:
        bench = json.load(f)
    with open(bridge_path, encoding="utf-8") as f:
        bridge = json.load(f)
    return {
        "distill": {"base_url": _req("DISTILL_BASE_URL").rstrip("/"),
                    "api_key": _req("DISTILL_API_KEY"), "model": _req("DISTILL_MODEL")},
        "gbrain": {"mcp_url": _req("GBRAIN_MCP_URL"), "token_url": _req("GBRAIN_TOKEN_URL")},
        "paths": {"state_db": _req("BRIDGE_STATE_DB"),
                  "review_queue": os.environ.get("REVIEW_QUEUE_DIR", "infra/distill/review-queue"),
                  "audit_log": os.environ.get("DISTILL_AUDIT", "infra/distill/audit.log"),
                  "canon_db": _req("CASS_CANON_DB"),
                  "fingerprint": "contracts/cass-canonical.fingerprint",
                  "memory_md": os.environ.get("MEMORY_MD"),
                  "hygiene_out": os.environ.get("HYGIENE_OUT", "infra/distill/memory-hygiene.md"),
                  "lock": os.environ.get("BRIDGE_LOCK", "/tmp/distill-bridge.lock")},
        "derived": bench["derived_config"],   # 单一来源（codex R0 P2-1 防漂移）：embed_batch_size/distill_concurrency/distill_timeout_s
        "budget": bridge["budget"],
        "contradiction_check": bridge.get("contradiction_check", True),
    }
