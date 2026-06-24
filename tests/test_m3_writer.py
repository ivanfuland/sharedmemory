import json, pytest
from distill import writer, state, idempotency

class FakeGbrain:
    """内存模拟 gbrain：put_page 建页、add_timeline_entry 原生去重、search 只索引 body(P1-4)、get_page/get_timeline 返回。
    get_timeline 返回真实 gbrain 形态：list of {"date":..., "summary":...}。"""
    def __init__(self): self.pages={}; self.timelines={}
    def __call__(self, cfg, token, tool, args):
        if tool=="put_page": self.pages[args["slug"]]=args["content"]; self.timelines.setdefault(args["slug"],[]); return {"ok":True}
        if tool=="add_timeline_entry":
            entry={"date": args["date"], "summary": args.get("summary","")}
            tl=self.timelines.setdefault(args["slug"],[])
            # 去重：(date, summary) 相同则跳过
            if not any(e["date"]==entry["date"] and e["summary"]==entry["summary"] for e in tl):
                tl.append(entry)
            return {"ok":True}
        if tool=="get_page": return {"text": self.pages.get(args["slug"],"")}
        if tool=="get_timeline": return self.timelines.get(args["slug"], [])   # 真实 gbrain 形态：list of dicts
        if tool=="search":
            def _body(md):
                p=md.split("---",2); return p[2] if md.startswith("---") and len(p)==3 else md
            q=args["query"]; hits=[s for s,c in self.pages.items() if q in _body(c)]   # body-only(去 frontmatter, codex R1 P1-1)
            return {"text":"\n".join(f"[1.0] {s} -- snippet" for s in hits)}
        raise AssertionError(f"unknown tool {tool}")

def _cfg(tmp_path):
    return {"distill":{"base_url":"x","api_key":"x","model":"gpt-5.4-mini"},
            "gbrain":{"mcp_url":"x","token_url":"x"},
            "paths":{"review_queue":str(tmp_path/"rq"),"audit_log":str(tmp_path/"audit.log")},
            "contradiction_check": False}

def _jrow(fact="决定用 X", slug="decisions/用 X 方案"):
    key=idempotency.fact_key("s:1#1", slug, "decision", fact)
    return {"key":key,"entity_slug":slug,"entry_type":"decision","fact_text":fact,
            "source_ref":"s:1#1","entry_date":"2026-06-24"}

def test_entry_text_embeds_provenance_and_key_marker():
    k=idempotency.fact_key("s:1#1","decisions/x","decision","决定用 X")
    t=writer.entry_text("决定用 X","s:1#1",k)
    assert "来源：s:1#1" in t and idempotency.key_marker(k) in t

def test_write_new_entity_creates_page_then_timeline(tmp_path):
    fake=FakeGbrain(); c=state.connect(str(tmp_path/"s.db"))
    jr=_jrow()
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES(?,1,?,?,?,?,?,'pending','2026-06-24')",
              (jr["key"],jr["entity_slug"],jr["entry_type"],jr["fact_text"],jr["source_ref"],jr["entry_date"])); c.commit()
    r=writer.write_entry(_cfg(tmp_path),"tok",c,jr,_call=fake)
    assert r=="done_new"
    assert jr["entity_slug"] in fake.pages                         # 新建页
    assert any(idempotency.key_marker(jr["key"]) in e["summary"] for e in fake.timelines[jr["entity_slug"]])  # 条目落 + 带 key
    assert c.execute("SELECT status FROM journal WHERE key=?", (jr["key"],)).fetchone()[0]=="done"

def test_write_existing_entity_appends_no_dup_page(tmp_path):
    fake=FakeGbrain(); c=state.connect(str(tmp_path/"s.db"))
    fake.pages["decisions/用 X 方案"]="---\ntitle: 用 X 方案\n---\n用 X 方案"   # 已存在
    fake.timelines["decisions/用 X 方案"]=[]
    jr=_jrow()
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES(?,1,?,?,?,?,?,'pending','2026-06-24')",
              (jr["key"],jr["entity_slug"],jr["entry_type"],jr["fact_text"],jr["source_ref"],jr["entry_date"])); c.commit()
    before=dict(fake.pages)
    writer.write_entry(_cfg(tmp_path),"tok",c,jr,_call=fake)
    assert fake.pages==before                                       # 不重建页（命中既有）

def test_multi_hit_goes_to_review_queue(tmp_path):
    fake=FakeGbrain(); c=state.connect(str(tmp_path/"s.db"))
    fake.pages["people/张三"]="张三 abc"; fake.pages["people/张三-2"]="张三 def"  # 多命中
    fake.timelines["people/张三"]=[]; fake.timelines["people/张三-2"]=[]
    jr=_jrow(fact="张三 喜欢 X", slug="people/张三")
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES(?,1,?,?,?,?,?,'pending','2026-06-24')",
              (jr["key"],jr["entity_slug"],jr["entry_type"],jr["fact_text"],jr["source_ref"],jr["entry_date"])); c.commit()
    r=writer.write_entry(_cfg(tmp_path),"tok",c,jr,_call=fake)
    assert r=="review_queued"
    import os; assert os.path.isdir(_cfg(tmp_path)["paths"]["review_queue"])

def test_search_is_body_only_not_frontmatter(tmp_path):
    fake=FakeGbrain()
    fake.pages["people/ghost"]="---\ntitle: ghost\naliases: [幽灵]\n---\n正文无别名词"
    assert writer.search_slugs(_cfg(tmp_path),"tok","幽灵",_call=fake)==[]   # 别名仅在 frontmatter → 不命中（body-only, R1 P1-1）

def test_alias_hit_goes_to_review_not_dup(tmp_path):
    fake=FakeGbrain(); c=state.connect(str(tmp_path/"s.db"))
    fake.pages["people/Ivan"]=writer.page_markdown("Ivan","people",["Ivan","老兰"],["s:1"],"2026-06-24")  # 别名"老兰"经 body-mirror 入 body
    fake.timelines["people/Ivan"]=[]
    jr=_jrow(fact="老兰 喜欢 X", slug="people/老兰")     # 候选用别名 → slug 不同
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES(?,1,?,?,?,?,?,'pending','2026-06-24')",
              (jr["key"],jr["entity_slug"],jr["entry_type"],jr["fact_text"],jr["source_ref"],jr["entry_date"])); c.commit()
    r=writer.write_entry(_cfg(tmp_path),"tok",c,jr,_call=fake)
    assert r=="review_queued"                       # 别名命中既有页(body-mirror) → review，不建重复页（codex R2 P1-3）
    assert "people/老兰" not in fake.pages
