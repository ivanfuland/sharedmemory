import json, os, pathlib, re, subprocess, uuid
import pytest

GH = os.environ.get("GBRAIN_HOME")
pytestmark = pytest.mark.needs_gbrain

@pytest.fixture(scope="module", autouse=True)
def _need_key():
    # 守门：openrouter recipe 嵌入经 LiteLLM 需 OPENROUTER_BASE_URL + OPENROUTER_API_KEY（从 config.env source），缺则 fail-loud
    for k in ("OPENROUTER_BASE_URL", "OPENROUTER_API_KEY"):
        assert os.environ.get(k), f"缺 {k}，先 `set -a; source infra/gbrain/config.env; set +a`"

def _gb(*args, stdin=None):
    r = subprocess.run(["gbrain", *args], input=stdin, capture_output=True, text=True,
                       timeout=180, env={**os.environ})   # OPENROUTER_BASE_URL/KEY 已在 env → 嵌入走 LiteLLM
    assert r.returncode == 0, f"gbrain {args} 失败: {r.stderr.strip()}"
    return r.stdout

def _slugs(out):
    # 解析 "[score] slug -- snippet" 的 slug 列；不对全文做 substring（snippet 可能含 slug 片段）
    return [m.group(1) for m in re.finditer(r"^\[[^\]]+\]\s+(\S+)\s+--", out, re.M)]

def test_config_dims_and_route():
    cfg = json.loads((pathlib.Path(GH) / ".gbrain" / "config.json").read_text())
    assert cfg.get("embedding_dimensions") == 1536, f"dims={cfg.get('embedding_dimensions')}（期望 1536）"
    # 必须走 openrouter recipe（openai-compatible，才认 OPENROUTER_BASE_URL 路由 LiteLLM；native openai 写死官网）
    assert str(cfg.get("embedding_model", "")).startswith("openrouter:"), \
        f"嵌入须走 openrouter recipe 才路由 LiteLLM: {cfg.get('embedding_model')}"

def test_embed_roundtrip_proves_vector_path():
    """★硬门：**唯一 slug**（uuid）确保每跑都是新页 → `gbrain put` 必发起真嵌入（put 自动 embed）。
    端点/key 坏时：put 走 noEmbed（无向量），随后 `gbrain embed` 强制嵌入并 fail-loud(exit 1) → 测试炸；
    即便绕过，零字符重叠 query（--no-expand，词法无从命中）也找不到该页 → hit[0]!=slug → 测试炸。
    固定 slug 会 fake-green（重跑时 put 无内容变更 no-op、旧向量仍在；codex PR review P0 实证）。
    distractor + 语义 query 把【光合作用】排第一 = 向量召回正确（排除退化向量）。用完 delete 清理。"""
    uid = uuid.uuid4().hex[:8]
    slug, dist = f"topics/光合作用-{uid}", f"topics/股票交易-{uid}"
    try:
        _gb("put", slug, stdin="# 光合作用\n绿叶在阳光下把二氧化碳转化为糖分。\n")   # put 自动嵌入（真打 LiteLLM）
        _gb("put", dist, stdin="# 股票交易\n证券市场买卖股票赚取价差收益\n")   # 无关 distractor，零字符重叠
        _gb("embed", slug); _gb("embed", dist)         # 端点坏时（put noEmbed）这里强制嵌入并 fail-loud
        hit = _slugs(_gb("query", "--no-expand", "植物如何制造养料"))   # 与两页正文均零字符重叠
        assert hit and hit[0] == slug, f"语义最近页应排第一（向量没通/退化？）: {hit!r}"
    finally:
        for s in (slug, dist):
            subprocess.run(["gbrain", "delete", s], capture_output=True, text=True, env={**os.environ})
