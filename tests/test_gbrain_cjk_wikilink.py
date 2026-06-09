"""中文 wikilink 连边硬门（spec §5 P0 出口④）。
实测：gbrain 0.42 中文 slug 页可建，body 里 [[中文slug]] 自动抽成 directional backlink，
backlinks --json 返回 {from_slug,to_slug,link_type} 结构化数组。失败即 M0 fail。"""
import json
import os
import subprocess
import uuid
import pytest

GBRAIN_HOME = os.environ["GBRAIN_HOME"]
pytestmark = pytest.mark.needs_gbrain


def _env():
    return {**os.environ, "GBRAIN_HOME": GBRAIN_HOME,
            "PATH": os.path.expanduser("~/.bun/bin") + ":" + os.environ.get("PATH", "")}


def _run(*args, stdin=None):
    r = subprocess.run(["gbrain", *args], input=stdin, capture_output=True,
                       text=True, env=_env())
    if r.returncode != 0:
        raise RuntimeError(f"gbrain {args}: {r.stderr or r.stdout}")
    return r.stdout


def test_chinese_wikilink_creates_directional_backlink():
    pid = uuid.uuid4().hex[:6]
    person = f"people/张三-{uuid.uuid4().hex[:6]}"
    project = f"projects/共享记忆层-{pid}"  # 独立后缀，person body 绝不含 pid
    _run("put", person, stdin="# 张三\n")
    # 控制组：建链前 person 无指向 project 的入边
    before = json.loads(_run("backlinks", person, "--json") or "[]")
    assert not any(pid in str(b.get("from_slug", "")) for b in before), "建链前已有入边——前置污染"
    # project body 用中文 wikilink 链向 person
    _run("put", project, stdin=f"# 共享记忆层\n\n负责人 [[{person}]]\n")
    # 断言：person 的入边来源含 project（方向 project→person，结构化）
    after = json.loads(_run("backlinks", person, "--json") or "[]")
    sources = [str(b.get("from_slug", "")) for b in after]
    assert any(pid in s for s in sources), (
        f"person 入边来源 {sources} 未含 project（中文 slug wikilink 反链失败）——spec §5 P0 出口④ FAIL"
    )
