# tests/test_m4_gold_gen.py
from distill import gold_gen
SPAN=[{"idx":0,"role":"user","content":"老兰决定 LFT 用 Qlib，不用 backtrader","source_path":"/x"}]
def _router(a,b,false_facts,dedup_out,dups):
    import re
    def chat(body,cfg):
        s=body["messages"][0]["content"]; model=body["model"]; u=body["messages"][-1]["content"]
        if "抽取" in s: return {"atoms": a if model=="MA" else b}
        if "对齐" in s: return {"matched": 1}
        if "去重" in s: return {"atoms": dedup_out}
        if "接地" in s:
            fact=(re.search(r'"fact"\s*:\s*"([^"]+)"',u) or [None,""])[1]
            return {"faithful": fact not in false_facts}
        if "重复审计" in s: return {"dups": dups}
        raise AssertionError(s[:30])
    return chat
CFG={"goldgen":{"model_a":"MA","model_b":"MB","base_url":"u","api_key":"k","temp_a":1,"temp_b":0},
     "judge":{"model":"MJ","base_url":"u","api_key":"k"}}
def test_faithful_last_kills_phantom():
    a=[{"entity":"LFT","fact":"底座用 Qlib"},{"entity":"LFT","fact":"不用 backtrader"}]
    b=[{"entity":"LFT","fact":"幻觉用 Python"}]
    dedup=[{"entity":"LFT","fact":"底座用 Qlib"},{"entity":"LFT","fact":"不用 backtrader"},{"entity":"LFT","fact":"幻觉用 Python"}]
    out=gold_gen.build_gold(SPAN,CFG,chat=_router(a,b,{"幻觉用 Python"},dedup,[]))
    assert {g["fact"] for g in out["atoms"]}=={"底座用 Qlib","不用 backtrader"}
def test_duplicate_audit_triggers_redup():
    a=[{"entity":"LFT","fact":"底座用 Qlib"}]; b=[{"entity":"LFT","fact":"底座选 Qlib"}]
    # 第一次 dedup 漏并（返回两条），dup 审计报 [[0,1]] → 再 dedup（这次合 1）
    calls={"dedup":0}
    import re
    def chat(body,cfg):
        s=body["messages"][0]["content"]; model=body["model"]; u=body["messages"][-1]["content"]
        if "抽取" in s: return {"atoms": a if model=="MA" else b}
        if "对齐" in s: return {"matched":1}
        if "去重" in s:
            calls["dedup"]+=1
            return {"atoms":[{"entity":"LFT","fact":"底座用 Qlib"},{"entity":"LFT","fact":"底座选 Qlib"}]} if calls["dedup"]==1 else {"atoms":[{"entity":"LFT","fact":"底座用 Qlib"}]}
        if "接地" in s: return {"faithful":True}
        if "重复审计" in s:
            return {"dups":[[0,1]]} if calls["dedup"]==1 else {"dups":[]}
        raise AssertionError(s[:30])
    out=gold_gen.build_gold(SPAN,CFG,chat=chat)
    assert out["agreement"]["residual_dups"]==0 and len(out["atoms"])==1
def test_extract_rejects_bad_shape():
    import pytest
    with pytest.raises(AssertionError):
        gold_gen.extract_atoms(SPAN,"MA","u","k",1,chat=lambda b,c:{"atoms":[{"entity":"E"}]})
def test_chat_retry_recovers():
    n={"c":0}
    def flaky(b,c):
        n["c"]+=1
        if n["c"]<2: raise RuntimeError("x")
        return {"ok":1}
    assert gold_gen._chat_retry({"model":"M","messages":[{"content":"x"}]},{},flaky,attempts=3)=={"ok":1}
