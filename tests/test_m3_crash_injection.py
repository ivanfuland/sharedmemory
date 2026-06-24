import pytest
from distill import run, state, reconcile, distiller, idempotency
from tests.test_m3_writer import FakeGbrain
from tests.test_m3_read_phase import _canon
from tests.test_m3_run import _cfg
from distill import cass_reader

def _prep(tmp_path):
    canon=_canon(tmp_path); cfg=_cfg(tmp_path, canon)
    open(cfg["paths"]["fingerprint"],"w").write(cass_reader._schema_fingerprint(canon))
    return cfg, canon

CHAT=lambda b,c:{"candidates":[{"entity_name":"X方案","entity_kind":"decision",
                 "entry_type":"decision","fact_text":"决定用 X","source_idx":0}]}

def test_bp1_crash_after_read_backlog_visible_then_resumes(tmp_path, monkeypatch):
    cfg,canon=_prep(tmp_path)
    c=state.connect(cfg["paths"]["state_db"])
    cass_reader.read_spans(canon, c, "ubuntu-cc","claude_code", 100)   # 只跑 read，然后"崩"
    assert state.total_backlog(c)["raw_backlog"]==1                    # read 后崩溃可见（不静默漏报）
    c.close()
    monkeypatch.setattr("distill.writer.load_token", lambda x:"t")
    monkeypatch.setattr("distill.writer.probe_tools", lambda x,t:{"put_page","add_timeline_entry","search","get_timeline","get_page"})
    rep=run.run_batch(cfg,[("ubuntu-cc","claude_code")],"2026-06-24",_chat=CHAT,_call=FakeGbrain())
    assert rep["processed_count"]==1 and rep["total_backlog"]["total_backlog"]==0  # 续跑零丢失

def test_bp2_crash_before_distill_txn_raw_stays_new(tmp_path):
    cfg,canon=_prep(tmp_path); c=state.connect(cfg["paths"]["state_db"])
    cass_reader.read_spans(canon,c,"ubuntu-cc","claude_code",100)
    # distill 抛错（事务前崩）→ raw 仍 new
    rid=c.execute("SELECT id FROM raw_work_item").fetchone()[0]
    with pytest.raises(Exception):
        distiller.distill_span([{"idx":0,"role":"user","content":"x","source_path":"/p"}], cfg,
                               _chat=lambda b,cc:(_ for _ in ()).throw(AssertionError("boom")))
    assert c.execute("SELECT status FROM raw_work_item WHERE id=?",(rid,)).fetchone()[0]=="new"

def test_bp3_crash_after_commit_before_write_then_write_lands(tmp_path, monkeypatch):
    cfg,canon=_prep(tmp_path); c=state.connect(cfg["paths"]["state_db"])
    cass_reader.read_spans(canon,c,"ubuntu-cc","claude_code",100)
    rid=c.execute("SELECT id FROM raw_work_item").fetchone()[0]
    distiller.commit_distilled(c, rid, CHAT(None,None)["candidates"], "/p/sess.jsonl")  # commit 后崩（journal=pending）
    assert c.execute("SELECT status FROM journal").fetchone()[0]=="pending"
    fake=FakeGbrain()
    from distill import writer
    jr=dict(c.execute("SELECT key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date FROM journal").fetchone())
    writer.write_entry(cfg,"t",c,jr,_call=fake)
    assert c.execute("SELECT status FROM journal").fetchone()[0]=="done"

def test_bp4_crash_after_gbrain_write_before_done_reconcile_no_dup(tmp_path):
    cfg,canon=_prep(tmp_path); c=state.connect(cfg["paths"]["state_db"])
    cass_reader.read_spans(canon,c,"ubuntu-cc","claude_code",100)
    rid=c.execute("SELECT id FROM raw_work_item").fetchone()[0]
    distiller.commit_distilled(c, rid, CHAT(None,None)["candidates"], "/p/sess.jsonl")
    jr=dict(c.execute("SELECT key,entity_slug FROM journal").fetchone())
    fake=FakeGbrain()                                                   # 模拟：gbrain 已落 key，但 journal 还 pending
    fake.pages[jr["entity_slug"]]="x"
    fake.timelines[jr["entity_slug"]]=[{"date":"2026-06-24","summary":f'决定用 X {idempotency.key_marker(jr["key"])}'}]
    res=reconcile.reconcile_pending(cfg,"t",c,_call=fake)
    assert res["already"]==1 and res["appended"]==0
    assert len(fake.timelines[jr["entity_slug"]])==1                    # 零重复落条
    assert c.execute("SELECT status FROM journal").fetchone()[0]=="done"

def test_bp5_crash_after_done_rerun_noop(tmp_path, monkeypatch):
    cfg,canon=_prep(tmp_path)
    monkeypatch.setattr("distill.writer.load_token", lambda x:"t")
    monkeypatch.setattr("distill.writer.probe_tools", lambda x,t:{"put_page","add_timeline_entry","search","get_timeline","get_page"})
    fake=FakeGbrain()
    run.run_batch(cfg,[("ubuntu-cc","claude_code")],"2026-06-24",_chat=CHAT,_call=fake)
    n1=len(next(iter(fake.timelines.values())))
    rep2=run.run_batch(cfg,[("ubuntu-cc","claude_code")],"2026-06-24",_chat=CHAT,_call=fake)  # 重跑
    n2=len(next(iter(fake.timelines.values())))
    assert n1==n2 and rep2["processed_count"]==0                        # 游标已过 + 无 pending → no-op

def test_bp6_budget_defer_then_next_day_processed(tmp_path, monkeypatch):
    cfg,canon=_prep(tmp_path); cfg["budget"]["batch_token_cap"]=0       # token 预算 0 → raw_deferred（codex R1 P0-3）
    monkeypatch.setattr("distill.writer.load_token", lambda x:"t")
    monkeypatch.setattr("distill.writer.probe_tools", lambda x,t:{"put_page","add_timeline_entry","search","get_timeline","get_page"})
    fake=FakeGbrain()
    run.run_batch(cfg,sources=[("ubuntu-cc","claude_code")],today="2026-06-24",_chat=CHAT,_call=fake)
    c=state.connect(cfg["paths"]["state_db"])
    assert c.execute("SELECT status FROM raw_work_item").fetchone()[0]=="raw_deferred"
    cfg["budget"]["batch_token_cap"]=200000                            # 次日放开
    rep2=run.run_batch(cfg,sources=[("ubuntu-cc","claude_code")],today="2026-06-25",_chat=CHAT,_call=fake)
    assert rep2["processed_count"]==1                                   # 次日 reset → 处理

def test_bp6b_journal_deferred_resets_next_day(tmp_path, monkeypatch):
    cfg,canon=_prep(tmp_path); cfg["budget"]["max_entities"]=0; cfg["budget"]["deferred_hard_cap"]=10
    c=state.connect(cfg["paths"]["state_db"])   # 预置 1 distilled raw + 1 pending journal（max_entities=0→read limit=0 不产 pending，codex R3 P1-1）
    c.execute("INSERT INTO raw_work_item(id,source_id,conversation_id,span_start,span_end,session_ref,status,created_at)"
              " VALUES(1,'ubuntu-cc',1,1,2,'/p/sess.jsonl','distilled','2026-06-24')")
    k=idempotency.fact_key("/p/sess.jsonl:0","decisions/X方案","decision","决定用 X")
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES(?,1,'decisions/X方案','decision','决定用 X','/p/sess.jsonl:0','2026-06-24','pending','2026-06-24')",(k,))
    c.commit(); c.close()
    monkeypatch.setattr("distill.writer.load_token", lambda x:"t")
    monkeypatch.setattr("distill.writer.probe_tools", lambda x,t:{"put_page","add_timeline_entry","search","get_timeline","get_page"})
    fake=FakeGbrain()
    run.run_batch(cfg,sources=[],today="2026-06-24",_chat=CHAT,_call=fake)     # max_entities=0 → 写预算 0 → journal deferred
    c=state.connect(cfg["paths"]["state_db"])
    assert c.execute("SELECT status FROM journal").fetchone()[0]=="deferred"
    c.close()
    cfg["budget"]["max_entities"]=400                                          # 次日放开
    run.run_batch(cfg,sources=[],today="2026-06-25",_chat=CHAT,_call=fake)     # 次日 reset→pending→写
    c=state.connect(cfg["paths"]["state_db"])
    assert c.execute("SELECT status FROM journal").fetchone()[0]=="done"

def test_bp7_quarantine_replay_processed(tmp_path):
    cfg,canon=_prep(tmp_path); c=state.connect(cfg["paths"]["state_db"])
    c.execute("INSERT INTO raw_work_item(id,source_id,conversation_id,span_start,span_end,session_ref,status,created_at)"
              " VALUES(1,'ubuntu-cc',1,1,2,'/p#1-2','raw_quarantined','2026-06-24')"); c.commit()
    state.replay_raw(c,1)                                               # 人工放行 → new
    assert c.execute("SELECT status FROM raw_work_item WHERE id=1").fetchone()[0]=="new"
    # journal 层 quarantine replay
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES('kq',1,'p/x','fact','f','r','2026-06-24','quarantined','2026-06-24')"); c.commit()
    reconcile.replay_quarantined(c, keys=["kq"])
    assert c.execute("SELECT status FROM journal WHERE key='kq'").fetchone()[0]=="pending"
