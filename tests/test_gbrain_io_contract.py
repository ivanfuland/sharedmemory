"""GBrain 0.42 写读契约（真实 text-CLI API；非 plan 的 JSON 假设）。
PGLite 沙盒内自建自清。命令/行为来自 contracts/gbrain-io-fields.json。
GBRAIN_HOME 由 conftest 默认；缺沙盒由 needs_gbrain 守门 fail。"""
import json
import os
import re
import subprocess
import uuid
import pathlib
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
F = json.loads((REPO / "contracts" / "gbrain-io-fields.json").read_text())
CMD = F["cmd"]
GBRAIN_HOME = os.environ["GBRAIN_HOME"]
pytestmark = pytest.mark.needs_gbrain


def _env():
    return {**os.environ, "GBRAIN_HOME": GBRAIN_HOME,
            "PATH": os.path.expanduser("~/.bun/bin") + ":" + os.environ.get("PATH", "")}


def _run(*args, stdin=None):
    r = subprocess.run(["gbrain", *args], input=stdin, capture_output=True,
                       text=True, env=_env())
    if r.returncode != 0:
        raise RuntimeError(f"gbrain {args} 失败: {r.stderr or r.stdout}")
    return r.stdout


def _put(slug, body):
    _run(CMD["put"], slug, stdin=body)


def _timeline_add(slug, date, text):
    _run(CMD["timeline_add"], slug, date, text)


def _timeline_lines(slug):
    return [l for l in _run(CMD["timeline"], slug).splitlines() if l.strip()]


def _search_lines(query):
    return [l for l in _run(CMD["search"], query).splitlines() if l.strip()]


@pytest.fixture
def slug():
    return f"people/contract-{uuid.uuid4().hex[:8]}"


def test_class1_put_timeline_get_success(slug):
    """第1类：put 整页 markdown + timeline-add + get 返回 body + timeline 见条目。"""
    body = f"# 测试\n\nbody-{uuid.uuid4().hex[:6]}\n"
    _put(slug, body)
    key = uuid.uuid4().hex[:12]
    _timeline_add(slug, "2026-06-09", f"事实 [dk:{key}]")
    got = _run(CMD["get"], slug)
    assert "测试" in got, "get 应返回页 body"
    assert any(key in l for l in _timeline_lines(slug)), "timeline 应含刚加的条目"


def test_class2_key_retrievable_via_timeline(slug):
    """第2类：idempotency key 回查走 timeline 文本扫描（search 不索引 timeline，实测发现）。"""
    _put(slug, "# k\n\nbody\n")
    key = uuid.uuid4().hex[:12]
    _timeline_add(slug, "2026-06-09", f"可回查 [dk:{key}]")
    assert any(key in l for l in _timeline_lines(slug)), (
        f"key {key} 应能在 timeline 文本里回查到（蒸馏桥 reconcile 据此）"
    )


def test_class3_timeline_native_dedup(slug):
    """第3类：同 (date,text) 两次 → timeline 只 1 条（原生去重，实测）。"""
    assert F["timeline_native_dedup"] is True
    _put(slug, "# d\n\nbody\n")
    key = uuid.uuid4().hex[:12]
    entry = ("2026-06-09", f"幂等 [dk:{key}]")
    _timeline_add(slug, *entry)
    _timeline_add(slug, *entry)  # 重复（模拟崩溃重跑）
    hits = [l for l in _timeline_lines(slug) if key in l]
    assert len(hits) == 1, f"同条目应去重为 1，实际 {len(hits)}"


def test_class4_conflict_both_timeline_entries_retained(slug):
    """第4类：两条矛盾 fact 作为两条独立 timeline 条目并存（不 silent 覆盖）。"""
    _put(slug, "# c\n\nbody\n")
    k1, k2 = uuid.uuid4().hex[:8], uuid.uuid4().hex[:8]
    _timeline_add(slug, "2026-06-09", f"张三在雷火 [dk:{k1}]")
    _timeline_add(slug, "2026-06-10", f"张三在网易游学 [dk:{k2}]")
    lines = _timeline_lines(slug)
    e1 = [l for l in lines if k1 in l]
    e2 = [l for l in lines if k2 in l]
    assert e1, "旧 fact 被 silent 丢弃——第4类 FAIL"
    assert e2, "新 fact 不在场"
    assert set(e1).isdisjoint(e2), "f1/f2 应各占一条 timeline"


def test_class6_stale_marker_parseable_and_clean_control(slug):
    """第6类：原生 (stale) 标记可机械解析 + 干净页非 stale（控制组）。
    M0 沙盒无法同步强制 stale 正例（由 dream 周期算），正例验证 deferred 到 M3。
    本测试守住：①stale 标记格式可解析 ②刚 put 的干净页不被误判 stale。"""
    rx = re.compile(F["search_line_regex"])
    _put(slug, f"# clean\n\nuniq-{uuid.uuid4().hex[:8]} body only\n")
    # 用页 body 里的唯一词搜，命中本页
    uniq = _run(CMD["get"], slug)
    token = re.search(r"uniq-\w+", uniq).group(0)
    lines = _search_lines(token)
    mine = [l for l in lines if slug in l]
    assert mine, f"应能搜到刚建的页（token={token}）"
    m = rx.match(mine[0].strip())
    assert m, f"search 行格式应匹配契约 regex：{mine[0]!r}"
    # 控制组：干净 body-only 页不应标 stale
    assert not m.group("stale"), (
        f"干净页被标 (stale)——stale 信号判别力存疑：{mine[0]!r}"
    )
