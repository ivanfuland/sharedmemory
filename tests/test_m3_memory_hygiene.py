import os, json
from distill import memory_hygiene
from tests.test_m3_writer import FakeGbrain, _cfg

def test_parse_entries():
    md = "# MEMORY\n- 铁律：禁订阅 OAuth\n- 踩坑：长消息分块\n普通段落不算\n"
    e = memory_hygiene.parse_entries(md)
    assert e == ["铁律：禁订阅 OAuth", "踩坑：长消息分块"]

def test_analyze_dry_run_never_mutates(tmp_path):
    mem = tmp_path / "MEMORY.md"; mem.write_text("- 长消息分块教训\n- 全新条目无重复\n", encoding="utf-8")
    fake = FakeGbrain(); fake.pages["projects/长消息分块教训"] = "长消息分块教训 详情"  # 制造一个疑似重复
    out = tmp_path / "proposal.md"
    res = memory_hygiene.analyze(_cfg(tmp_path), "tok", str(mem), str(out), _call=fake)
    assert res["entries"] == 2
    assert res["proposals"] >= 1
    assert out.exists()
    assert mem.read_text(encoding="utf-8") == "- 长消息分块教训\n- 全新条目无重复\n"   # 源文件零改动
