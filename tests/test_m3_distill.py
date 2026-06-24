import pytest
from distill import distiller, idempotency, state

def test_fact_key_stable_and_collision_safe():
    k1 = idempotency.fact_key("s:1#1-9", "people/张三", "fact", "张三 喜欢 X")
    k2 = idempotency.fact_key("s:1#1-9", "people/张三", "fact", "张三  喜欢 X")  # 空白差异
    k3 = idempotency.fact_key("s:1#1-9", "people/张三", "fact", "张三 喜欢 Y")  # 不同事实
    assert k1 == k2          # normalize 后同
    assert k1 != k3          # 不同 fact_text → 不同 key（防静默丢事实）
    assert idempotency.key_marker(k1) == f"[dk:{k1[:16]}]"

def test_slug_for_maps_kind_to_dir():
    assert idempotency.slug_for("person", "张三") == "people/张三"
    assert idempotency.slug_for("project", "共享记忆层") == "projects/共享记忆层"
    assert idempotency.slug_for("decision", "用 X 方案") == "decisions/用 x 方案"   # 小写化（gbrain slug 要小写，e2e 实测）
    # 大写/路径分隔回归（e2e：真 gbrain put_page 对大写 slug 报 "Page not found"）
    assert idempotency.slug_for("project", "M1 Plan Strict-Routing") == "projects/m1 plan strict-routing"
    assert idempotency.slug_for("project", "client/server") == "projects/client-server"

def test_distill_drops_candidate_without_valid_provenance():
    rows = [{"idx": 0, "role": "user", "content": "我们决定用 X", "source_path": "/p/s.jsonl"},
            {"idx": 1, "role": "assistant", "content": "好", "source_path": "/p/s.jsonl"}]
    fake = {"candidates": [
        {"entity_name": "X方案", "entity_kind": "decision", "entry_type": "decision",
         "fact_text": "决定用 X", "source_idx": 0},                       # 有效（idx 在 span）
        {"entity_name": "幻觉", "entity_kind": "project", "entry_type": "fact",
         "fact_text": "瞎编的", "source_idx": 99},                        # 无效 idx → 丢弃 + 计数
    ]}
    out = distiller.distill_span(rows, _cfg(), _chat=lambda body, cfg: fake)
    assert len(out["candidates"]) == 1
    assert out["rejected_no_provenance"] == 1

def test_distill_retries_twice_then_raises():
    calls = {"n": 0}
    def boom(body, cfg):
        calls["n"] += 1; raise AssertionError("provider down")
    with pytest.raises(AssertionError):
        distiller.distill_span([{"idx":0,"role":"user","content":"x","source_path":"/p"}], _cfg(), _chat=boom)
    assert calls["n"] == 3   # 1 + retry×2

def test_distill_empty_sentinel_ok():
    out = distiller.distill_span([{"idx":0,"role":"user","content":"HEARTBEAT_OK","source_path":"/p"}],
                                 _cfg(), _chat=lambda body, cfg: {"candidates": []})
    assert out["candidates"] == [] and out["rejected_no_provenance"] == 0

def test_commit_distilled_single_txn(tmp_path):
    c = state.connect(str(tmp_path / "s.db"))
    c.execute("INSERT INTO raw_work_item(id,source_id,conversation_id,span_start,span_end,session_ref,status,created_at)"
              " VALUES(7,'ubuntu-cc',1,1,9,'/p/s.jsonl#1-9','new','2026-06-24')"); c.commit()
    n = distiller.commit_distilled(c, 7,
        [{"entity_name":"X方案","entity_kind":"decision","entry_type":"decision","fact_text":"决定用 X","source_idx":1}],
        "/p/s.jsonl")
    assert n == 1
    assert c.execute("SELECT status FROM raw_work_item WHERE id=7").fetchone()[0] == "distilled"
    assert c.execute("SELECT status FROM journal").fetchone()[0] == "pending"

def test_distill_chunks_long_message_no_truncation():
    long = "决定用 A。" * 6000   # ~36000 chars > chunk_char_size
    rows = [{"idx":0,"role":"user","content":long,"source_path":"/p"}]
    seen = {"n": 0}
    def chat(body, cfg):
        seen["n"] += 1; return {"candidates": []}
    cfg = _cfg(); cfg["budget"]["chunk_char_size"] = 10000; cfg["budget"]["chunk_overlap"] = 200
    distiller.distill_span(rows, cfg, _chat=chat)
    assert seen["n"] >= 3   # 长消息被切多块逐块蒸馏（非一次截断丢中段）

def test_timeline_date_is_conversation_date_not_run_date(tmp_path):
    # backlog 蒸馏：timeline 日期必须取会话消息真实日期，不能盖跑批当天（e2e 抓到的 P0）
    from datetime import datetime, timezone
    old_ms = int(datetime(2026, 3, 18, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)  # 老会话 2026-03-18
    rows = [{"idx": 5, "role": "user", "content": "我们决定用 X 方案",
             "source_path": "/p/s.jsonl", "created_at": old_ms}]
    fake = {"candidates": [{"entity_name": "X方案", "entity_kind": "decision", "entry_type": "decision",
                            "fact_text": "决定用 X 方案", "source_idx": 5}]}
    out = distiller.distill_span(rows, _cfg(), _chat=lambda body, cfg: fake)
    c = state.connect(str(tmp_path / "s.db"))
    c.execute("INSERT INTO raw_work_item(id,source_id,conversation_id,span_start,span_end,session_ref,status,created_at)"
              " VALUES(7,'ubuntu-cc',1,1,9,'/p/s.jsonl#1-9','new','2026-06-24')"); c.commit()
    distiller.commit_distilled(c, 7, out["candidates"], "/p/s.jsonl")
    got = c.execute("SELECT entry_date FROM journal").fetchone()[0]
    assert got == "2026-03-18", f"timeline 日期应为会话真实日期 2026-03-18；got {got}（盖成跑批当天=bug）"


def test_msg_date_uses_gmt8_not_utc_at_day_boundary():
    # 2026-05-09 17:00 UTC = 2026-05-10 01:00 GMT+8 → 本地日期应为 05-10（消除傍晚偏移）
    from datetime import datetime, timezone
    ms = int(datetime(2026, 5, 9, 17, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert distiller._msg_date(ms) == "2026-05-10"


def _cfg():
    return {"distill": {"base_url": "x", "api_key": "x", "model": "gpt-5.4-mini"},
            "budget": {"chunk_char_size": 24000, "chunk_overlap": 400},
            "derived": {"distill_timeout_s": 90},
            "paths": {"audit_log": "/tmp/cc-m3-test-audit.log"}}
