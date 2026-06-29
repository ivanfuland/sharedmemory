# tests/test_m4_stale_json_object.py
from distill import stale
def test_assess_contradiction_uses_json_object_and_mentions_json():
    cap = {}
    def fake(body, cfg):
        cap["body"] = body
        return {"contradicts": True}
    cfg = {"contradiction_check": True, "distill": {"model": "deepseek-v4-flash"},
           "paths": {"audit_log": "/tmp/cc-t.log"}}
    md = "---\ntitle: x\n---\n# 标题\n服务 X 预算上限 10 万。"
    out = stale.assess_contradiction(cfg, "tok", "decisions/svc-x", "服务 X 不设预算上限",
                                     call=None, chat=fake, page_md=md)
    assert out is True
    assert cap["body"]["response_format"] == {"type": "json_object"}
    assert "json" in cap["body"]["messages"][0]["content"].lower()
