from distill import stale, writer, state, idempotency
from tests.test_m3_writer import FakeGbrain, _jrow, _cfg

def test_high_impact_detection():
    assert stale.is_high_impact("decisions/用 X 方案")
    assert stale.is_high_impact("preferences/审美")
    assert not stale.is_high_impact("people/张三")

def test_assess_contradiction_skips_stub_pages(tmp_path):
    fake=FakeGbrain()
    fake.pages["decisions/x"]=writer.page_markdown("x","decisions",["x"],["s:1"],"2026-06-24")  # 仅 stub
    cfg=_cfg(tmp_path); cfg["contradiction_check"]=True
    # stub 页无 compiled truth → 不判矛盾（不触发 LLM），返回 False
    assert stale.assess_contradiction(cfg,"tok","decisions/x","新事实",call=fake,
                                      chat=lambda b,c: {"contradicts": True}) is False

def test_assess_contradiction_flags_real_truth(tmp_path):
    fake=FakeGbrain()
    fake.pages["decisions/x"]="---\ntitle: x\n---\n# x\ncompiled: 我们用方案 A 不用 B。"  # 有真 truth
    cfg=_cfg(tmp_path); cfg["contradiction_check"]=True
    assert stale.assess_contradiction(cfg,"tok","decisions/x","其实改用方案 B 了",call=fake,
                                      chat=lambda b,c: {"contradicts": True}) is True

def test_judge_failure_non_fatal_write_still_succeeds(tmp_path):
    """I-1: judge chat raises → write_entry returns done_append (no flag, not quarantined/raised)."""
    import distill.distiller as _distiller
    from distill import state
    fake = FakeGbrain()
    c = state.connect(str(tmp_path / "s.db"))
    # seed a page with real compiled-truth body so _has_compiled_truth gate passes
    fake.pages["decisions/用 X 方案"] = "---\ntitle: 用 X 方案\n---\n# 用 X 方案\ncompiled: 我们用方案 A 不用 B。"
    fake.timelines["decisions/用 X 方案"] = []
    jr = _jrow(fact="其实改用方案 B 了", slug="decisions/用 X 方案")
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES(?,1,?,?,?,?,?,'pending','2026-06-24')",
              (jr["key"], jr["entity_slug"], jr["entry_type"], jr["fact_text"], jr["source_ref"], jr["entry_date"]))
    c.commit()
    cfg = _cfg(tmp_path)
    cfg["contradiction_check"] = True
    # patch _chat_http so the judge raises, proving I-1 makes it non-fatal
    orig = _distiller._chat_http
    _distiller._chat_http = lambda body, cfg: (_ for _ in ()).throw(RuntimeError("judge transport failure"))
    try:
        r = writer.write_entry(cfg, "tok", c, jr, _call=fake)
    finally:
        _distiller._chat_http = orig
    assert r == "done_append"
    assert c.execute("SELECT status FROM journal WHERE key=?", (jr["key"],)).fetchone()[0] == "done"
    tl = fake.timelines["decisions/用 X 方案"]
    assert tl and not any(stale.CONTRADICTS_FLAG in e["summary"] for e in tl)


def test_write_high_impact_queues_review(tmp_path):
    fake=FakeGbrain(); c=state.connect(str(tmp_path/"s.db"))
    jr=_jrow(fact="决定用 X", slug="decisions/用 X 方案")
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES(?,1,?,?,?,?,?,'pending','2026-06-24')",
              (jr["key"],jr["entity_slug"],jr["entry_type"],jr["fact_text"],jr["source_ref"],jr["entry_date"])); c.commit()
    cfg=_cfg(tmp_path); cfg["contradiction_check"]=False
    r=writer.write_entry(cfg,"tok",c,jr,_call=fake)
    assert r=="done_new"
    import os; assert os.listdir(cfg["paths"]["review_queue"])   # high-impact → 留 review 副本（不阻塞写）
