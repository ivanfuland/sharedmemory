import sqlite3
from distill import run, state, config as cfgmod
from tests.test_m3_writer import FakeGbrain
from tests.test_m3_read_phase import _canon

def _cfg(tmp_path, canon):
    return {"distill":{"base_url":"x","api_key":"x","model":"gpt-5.4-mini"},
            "gbrain":{"mcp_url":"x","token_url":"x"},
            "paths":{"state_db":str(tmp_path/"s.db"),"review_queue":str(tmp_path/"rq"),
                     "audit_log":str(tmp_path/"audit.log"),"canon_db":canon,
                     "fingerprint":str(tmp_path/"fp"),"lock":str(tmp_path/"b.lock")},
            "derived":{"distill_timeout_s":90},"contradiction_check":False,
            "budget":{"batch_token_cap":200000,"max_entities":400,"deferred_hard_cap":2000,"chunk_char_size":24000,"chunk_overlap":400}}

def test_end_to_end_one_batch(tmp_path, monkeypatch):
    canon=_canon(tmp_path)
    cfg=_cfg(tmp_path, canon)
    # 写真实指纹（让 verify 通过）
    from distill import cass_reader
    open(cfg["paths"]["fingerprint"],"w").write(cass_reader._schema_fingerprint(canon))
    fake=FakeGbrain()
    monkeypatch.setattr("distill.writer.load_token", lambda c: "tok")
    monkeypatch.setattr("distill.writer.probe_tools", lambda c,t: {"put_page","add_timeline_entry","search","get_timeline","get_page"})
    fake_chat=lambda body,c: {"candidates":[
        {"entity_name":"X方案","entity_kind":"decision","entry_type":"decision","fact_text":"决定用 X","source_idx":0}]}
    rep=run.run_batch(cfg, sources=[("ubuntu-cc","claude_code")], today="2026-06-24",
                      _chat=fake_chat, _call=fake)
    assert rep["processed_count"]==1                 # 1 raw → distilled（raw_processed 口径）
    assert rep["appended_entries"]>=1
    assert rep["total_backlog"]["total_backlog"]==0   # 全部落库
    assert "decisions/x方案" in fake.pages

def test_fingerprint_mismatch_is_fatal(tmp_path):
    canon=_canon(tmp_path); cfg=_cfg(tmp_path, canon)
    open(cfg["paths"]["fingerprint"],"w").write("wrong")
    import pytest
    with pytest.raises(Exception):
        run.run_batch(cfg, [("ubuntu-cc","claude_code")], "2026-06-24",
                      _chat=lambda b,c:{"candidates":[]}, _call=FakeGbrain())

def test_deferred_hard_cap_stops_bridge(tmp_path, monkeypatch):
    canon=_canon(tmp_path); cfg=_cfg(tmp_path, canon)
    from distill import cass_reader, state
    open(cfg["paths"]["fingerprint"],"w").write(cass_reader._schema_fingerprint(canon))
    c=state.connect(cfg["paths"]["state_db"])   # 预置积压：1 raw distilled + 1 pending journal（codex R2 P2-1：max_entities=0 会让 read 读零行，须直接预置）
    c.execute("INSERT INTO raw_work_item(id,source_id,conversation_id,span_start,span_end,session_ref,status,created_at)"
              " VALUES(1,'ubuntu-cc',1,1,2,'s','distilled','2026-06-24')")
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES('k',1,'people/x','fact','f','s','2026-06-24','pending','2026-06-24')")
    c.commit(); c.close()
    cfg["budget"]["max_entities"]=0          # 写预算 0 → pending 被 defer
    cfg["budget"]["deferred_hard_cap"]=0     # 任何 deferred 即超 → 停桥（codex R0 P0-5）
    monkeypatch.setattr("distill.writer.load_token", lambda c:"t")
    monkeypatch.setattr("distill.writer.probe_tools", lambda c,t:{"put_page","add_timeline_entry","search","get_timeline","get_page"})
    import pytest
    with pytest.raises(SystemExit):
        run.run_batch(cfg, sources=[], today="2026-06-24",
                      _chat=lambda b,c:{"candidates":[]}, _call=FakeGbrain())
