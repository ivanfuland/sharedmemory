"""P4 §Task 3:token-aware passage 组装(标定与生产必须同口径)。

CAP 公式(冻结,P4 §窗口预注册,改公式=改 spec):
    CAP = min(embed_window, rerank_window - query_budget, HARD_CAP)

- embed_window / rerank_window:cc-infinity 两模型(BAAI/bge-m3 embed、
  BAAI/bge-reranker-v2-m3 rerank)各自的有效 token 窗口。Step 0 先 live
  `GET $INFINITY_BASE/models` 核对两模型确实在服务(活性核实,不是纸面假设),
  再从本机 pinned HF snapshot 的 tokenizer_config.json `model_max_length`
  读窗口值——比 config.json 声明的 `max_position_embeddings`(8194)少 2,
  那 2 位是 xlm-roberta 绝对位置编码的保留位(pad_token_id 起算的偏移),
  不是可用文本窗;实测两模型 tokenizer 均声明 model_max_length=8192。
- query_budget:tokenize(150 个中文字符的极端 query,契约允许的最长 query)
  实算的 token 数(含 special tokens),不写死 128(R4)。实测 150 中文字符
  → 153 token(2026-07-16 本机实测,BAAI/bge-reranker-v2-m3 pinned tokenizer)。

截断口径统一用 rerank(CE,cross-encoder)侧 tokenizer——CE 打分吃的是
query+passage 对,预算以 rerank 侧 token 计数为唯一真值;embed 侧 tokenizer
只参与 CAP 公式的 min 项,不用于实际截断。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from urllib.request import urlopen

try:
    from transformers import AutoTokenizer
except ImportError as e:  # fail-loud(不静默退回字符截断——token-aware 是本模块唯一理由)
    raise ImportError(
        "everos_eval.probe_passage 需要 transformers/tokenizers"
        "(uv run --group probe ...)。缺依赖时必须停工,不允许静默退回字符截断"
        "——中文卡在字符截断下实测会超模型窗。"
    ) from e


# ---- Step 0 冻结值:pinned HF snapshot(local_files_only,revision 写死禁自动升级) ----
EMBED_MODEL_ID = "BAAI/bge-m3"
EMBED_MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"  # HF refs/main 实测(2026-07-16)

RERANK_MODEL_ID = "BAAI/bge-reranker-v2-m3"
# 2026-07-16 本机实测(仅拉 tokenizer+config,零权重,huggingface_hub.snapshot_download
# allow_patterns 排除 *.bin/*.safetensors):
RERANK_MODEL_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"

HARD_CAP = 2048  # P4 公式第三项:硬顶,防极端窗口值把 passage 撑到不合理长度

QUERY_BUDGET_CHAR_LEN = 150  # 契约允许的最长 query(中文字符数,R4 冻结输入,非产出)

# ---- Task 4 追加(P5 CE 对预算断言,Task 3 遗留点名):CE (query,doc) 对总 token 硬顶 ----
# 语义与 CAP 不同:CAP(=2048)只截 passage 单独文本;PAIR_BUDGET 是 query+passage 拼给
# cross-encoder 打分时的总 token 数真硬顶,真值 = rerank 侧模型有效窗口本身(非 CAP)。
# 2026-07-16 本机实测(见 task-3-report.md Step 0,tokenizer_config.json model_max_length):
# BAAI/bge-reranker-v2-m3 有效窗口 = 8192,与本模块 Step 0 实测的 RERANK_WINDOW 冻结值一致。
# 硬编码而非每次调用 `_tokenizer_window(rerank_tokenizer())` 现算:窗口值已随 revision pin
# 冻结为测量事实(同 EMBED_MODEL_REVISION/HARD_CAP 的既有风格),避免让每个仅需 PAIR_BUDGET
# 数值的调用方都背上一次 tokenizer 加载成本。若未来 revision 变动,须同步复核这个数值。
PAIR_BUDGET = 8192


@lru_cache(maxsize=None)
def _load_tokenizer(model_id: str, revision: str):
    """pinned HF snapshot、local_files_only=True——加载失败即停工,不静默降级。"""
    return AutoTokenizer.from_pretrained(model_id, revision=revision, local_files_only=True)


def rerank_tokenizer():
    """CE 截断基准 tokenizer(预算以 rerank 侧计数为唯一真值)。"""
    return _load_tokenizer(RERANK_MODEL_ID, RERANK_MODEL_REVISION)


def embed_tokenizer():
    return _load_tokenizer(EMBED_MODEL_ID, EMBED_MODEL_REVISION)


def _tokenizer_window(tokenizer) -> int:
    """有效 token 窗口 = tokenizer_config.json 的 model_max_length(已扣掉绝对位置
    编码保留位,不是 config.json 原始声明的 max_position_embeddings)。"""
    win = tokenizer.model_max_length
    if win is None or win > 1_000_000:  # HF 对"无限制"模型常填 int(1e30) 之类哨兵值
        raise ValueError(f"{tokenizer!r} 未声明有限 model_max_length,无法定窗口")
    return int(win)


def compute_query_budget(tokenizer=None) -> int:
    """query_budget := tokenize(150 个中文字符的极端 query)实算 token 数(含 special
    tokens)。150 是冻结的契约字符数上限,token 数每次都是真实分词结果,不写死。"""
    tokenizer = tokenizer or rerank_tokenizer()
    worst_case_query = "测" * QUERY_BUDGET_CHAR_LEN
    return len(tokenizer.encode(worst_case_query))


def compute_cap(embed_window: int, rerank_window: int, query_budget: int,
                 hard_cap: int = HARD_CAP) -> int:
    return min(embed_window, rerank_window - query_budget, hard_cap)


def fetch_infinity_models(infinity_base: str, get_json=None) -> list[str]:
    """GET $INFINITY_BASE/models 活性核实(Step 0):cc-infinity 实际在服务的模型 id 集。

    `get_json`(可选,默认 `None`):注入形如 `everos_mcp.http.get_json(url, timeout)
    -> dict` 的调用——同一出站通道(loopback 断言 + 拒绝 redirect),避免这个
    GET 探针绕开 everos_mcp 的出站边界另起裸 `urlopen`。默认 `None` 时行为与
    此前完全一致(裸 `urlopen`,零变化,供不需要该边界的调用方/既有测试用)。
    """
    if get_json is not None:
        body = get_json(f"{infinity_base}/models", 30)
    else:
        with urlopen(f"{infinity_base}/models", timeout=30) as resp:
            body = json.loads(resp.read().decode())
    return [item["id"] for item in body["data"]]


@dataclass(frozen=True)
class WindowProbe:
    embed_model_id: str
    embed_model_revision: str
    embed_window: int
    rerank_model_id: str
    rerank_model_revision: str
    rerank_window: int
    query_budget: int
    query_budget_char_len: int
    cap: int
    infinity_models_seen: tuple[str, ...]  # GET /models 实返回的 id 集(活性核实证据)

    def as_meta(self) -> dict:
        return {
            "embed_model_id": self.embed_model_id,
            "embed_model_revision": self.embed_model_revision,
            "embed_window": self.embed_window,
            "rerank_model_id": self.rerank_model_id,
            "rerank_model_revision": self.rerank_model_revision,
            "rerank_window": self.rerank_window,
            "query_budget": self.query_budget,
            "query_budget_char_len": self.query_budget_char_len,
            "cap": self.cap,
            "cap_formula": "min(embed_window, rerank_window - query_budget, 2048)",
            "infinity_models_seen": list(self.infinity_models_seen),
        }


def run_window_probe(infinity_base: str, get_json=None) -> WindowProbe:
    """Step 0:live 核实两模型仍在服务 + 本机 pinned tokenizer 读窗口 + 定 CAP。

    `get_json`:透传给 `fetch_infinity_models`(见其文档字符串)——默认 `None`
    时行为不变(裸 `urlopen`)。"""
    seen = fetch_infinity_models(infinity_base, get_json=get_json)
    for expected in (EMBED_MODEL_ID, RERANK_MODEL_ID):
        assert expected in seen, (
            f"cc-infinity /models 未见 {expected}(实返回 {seen})——"
            "窗口探针的前提(两模型确实在服务)不成立,CAP 不可信,停工。"
        )

    embed_win = _tokenizer_window(embed_tokenizer())
    rerank_win = _tokenizer_window(rerank_tokenizer())
    query_budget = compute_query_budget()
    cap = compute_cap(embed_win, rerank_win, query_budget)

    return WindowProbe(
        embed_model_id=EMBED_MODEL_ID,
        embed_model_revision=EMBED_MODEL_REVISION,
        embed_window=embed_win,
        rerank_model_id=RERANK_MODEL_ID,
        rerank_model_revision=RERANK_MODEL_REVISION,
        rerank_window=rerank_win,
        query_budget=query_budget,
        query_budget_char_len=QUERY_BUDGET_CHAR_LEN,
        cap=cap,
        infinity_models_seen=tuple(seen),
    )


# ---- 规格:prod / full ----

_PROD_CASE_FIELDS = ("task_intent", "approach")
_FULL_CASE_EXTRA = ("key_insight",)
_PROD_SKILL_FIELDS = ("name", "description")
_FULL_SKILL_EXTRA = ("content",)

_SPEC_DESC = {
    "prod": {
        "agent_case": "task_intent+\\n+approach",
        "agent_skill": "name+\\n+description",
    },
    "full": {
        "agent_case": "task_intent+\\n+approach+\\n+key_insight",
        "agent_skill": "name+\\n+description+\\n+content",
    },
}


def _fields_for(mem_type: str, spec: str) -> tuple[str, ...]:
    if mem_type == "agent_case":
        base, extra = _PROD_CASE_FIELDS, _FULL_CASE_EXTRA
    elif mem_type == "agent_skill":
        base, extra = _PROD_SKILL_FIELDS, _FULL_SKILL_EXTRA
    else:
        raise ValueError(f"unknown mem_type: {mem_type!r}(expected agent_case/agent_skill)")

    if spec == "prod":
        return base
    if spec == "full":
        return base + extra
    raise ValueError(f"unknown spec: {spec!r}(expected 'prod' or 'full')")


def _assemble_text(payload: dict, mem_type: str, spec: str) -> str:
    fields = _fields_for(mem_type, spec)
    return "\n".join(payload[field] for field in fields)  # 缺字段原生 KeyError,不静默填空


def _truncate_to_cap(text: str, cap: int, tokenizer) -> str:
    """token-safe 截断:纯内容 ids(不带 special tokens)截到 cap,decode 回文本;
    截断后重编码(带 special tokens)断言 ≤ cap+2(2 个特殊 token 的余量)。"""
    ids = tokenizer.encode(text, add_special_tokens=False)
    if len(ids) <= cap:
        return text
    truncated_ids = ids[:cap]
    truncated_text = tokenizer.decode(truncated_ids, skip_special_tokens=True)
    check_ids = tokenizer.encode(truncated_text, add_special_tokens=True)
    assert len(check_ids) <= cap + 2, (
        f"截断后重编码仍超预算:{len(check_ids)} tokens > cap({cap})+2"
    )
    return truncated_text


def build_passage(payload: dict, mem_type: str, spec: str = "prod", *,
                   cap: int, tokenizer=None) -> str:
    """组装 passage 文本(prod/full 规格,token 截断到 cap)。

    prod:case = task_intent+"\\n"+approach;skill = name+"\\n"+description。
    full:prod 基础上,case 追加 "\\n"+key_insight,skill 追加 "\\n"+content。
    缺字段抛 KeyError(不静默填空)。截断按 rerank tokenizer 的 token 计数,
    不按字符——中文字符数远小于 token 数上界时仍可能超模型窗(全角/中文平均
    ~1.3-2 token/字,1600 字符实测已超部分模型的窗)。
    """
    tokenizer = tokenizer or rerank_tokenizer()
    text = _assemble_text(payload, mem_type, spec)
    return _truncate_to_cap(text, cap, tokenizer)


def token_len(text: str, tokenizer=None, add_special_tokens: bool = False) -> int:
    """辅助:算一段文本(截断前的原始拼接文本)实际 token 数,供统计脚本判定
    「是否超 CAP 需要截断」用,不受 build_passage 内部截断逻辑影响。"""
    tokenizer = tokenizer or rerank_tokenizer()
    return len(tokenizer.encode(text, add_special_tokens=add_special_tokens))


def passage_spec_sha(spec: str, cap: int, mem_type: str,
                      tokenizer_name: str = RERANK_MODEL_ID,
                      tokenizer_revision: str = RERANK_MODEL_REVISION) -> str:
    """规格指纹:规格描述串 + cap + tokenizer 名 + tokenizer revision 的 sha256。
    公式/字段/CAP/tokenizer 版本任一变动都会改变这个 hash——供台账核验
    「标定用的是不是这一版规格」。"""
    if mem_type not in _SPEC_DESC.get(spec, {}):
        raise ValueError(f"unknown spec/mem_type combo: spec={spec!r}, mem_type={mem_type!r}")
    desc = _SPEC_DESC[spec][mem_type]
    payload = f"{spec}:{mem_type}={desc}|cap={cap}|tokenizer={tokenizer_name}@{tokenizer_revision}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
