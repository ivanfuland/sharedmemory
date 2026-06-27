# tests/test_m4_stale_json_object.py
from distill import stale
def test_assess_contradiction_uses_json_object_and_mentions_json():
    cap={}
    def fake(body,cfg): cap["body"]=body; return {"contradicts":True}
    cfg={"contradiction_check":True,"distill":{"model":"deepseek-v4-flash"},"paths":{"audit_log":"/tmp/cc-t.log"}}
    md="---\ntitle:x\n---\n# 标题\nLFT 资金封顶 10 万。"
    assert stale.assess_contradiction(cfg,"tok","decisions/lft","LFT 不设资金上限",call=None,chat=fake,page_md=md) is True
    assert cap["body"]["response_format"]=={"type":"json_object"}
    assert "json" in cap["body"]["messages"][0]["content"].lower()
