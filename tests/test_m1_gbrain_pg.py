import json, os, pathlib, re, subprocess
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
    """★硬门 = gbrain embed 的 fail-loud（embed.ts/gateway.ts：endpoint/key 错 → exit 1；
    维度 != 1536 → 插 vector(1536) 报错 → exit 1）。put/embed 链路 rc==0 = 经 LiteLLM 嵌入真通
    （openrouter recipe 唯一端点=OPENROUTER_BASE_URL=LiteLLM，无官网旁路 → 成功即证流量过 LiteLLM）。
    加无关 distractor，零字符重叠语义 query 经 query --no-expand 应把【光合作用】排第一
    （命中只能来自向量；distractor 排不过 = 排除退化向量）。FAIL 则停手查 LiteLLM 路由/key，不强推。"""
    slug = "topics/光合作用"
    _gb("put", slug, stdin="# 光合作用\n绿叶在阳光下把二氧化碳转化为糖分。\n")
    _gb("put", "topics/股票交易", stdin="# 股票交易\n证券市场买卖股票赚取价差收益\n")   # 无关 distractor，正文零字符重叠
    _gb("embed", slug); _gb("embed", "topics/股票交易")   # rc==0 即证真往返（fail-loud）
    hit = _slugs(_gb("query", "--no-expand", "植物如何制造养料"))   # 与两页正文均零字符重叠
    assert hit and hit[0] == slug, f"语义最近页应排第一（向量没通/退化？）: {hit!r}"
