"""P4/P5 §Task 4:打分底座(embed/rerank 客户端)+ 响应还原硬契约 + fail-closed
缓存 + known-control(P0-4 重设计,管线正确性与模型质量分离)。

**核心风险(P0-2,codex 抓出的命门)**:Infinity `/rerank` 的 `results` 按分数
降序返回,不是按输入序;`/embeddings` 的 `data` 理论上也不保证顺序。若直接
按响应数组的物理顺序取值,会把分数错配到别的候选卡上——产出的"哪张卡分高"
结论全错,但流程看起来跑得完全正常(不报错、不崩溃、指标数字照样能算出来)。
本模块的存在理由就是杜绝这件事:强制按 `item.index` scatter 回输入序,并对
index 集合做严格闭合校验(缺失/重复/越界一律拒),分数非有限值一律拒,批内
embedding 维度不一致一律拒。任一违反直接抛异常停工,绝不允许把错配/坏值悄悄
喂给下游判据引擎产出假指标。

**CE 对预算断言(Task 3 遗留点名)**:`rerank()` 对每个 (query, doc) 对在发请求
前先断言 token 总数 ≤ `probe_passage.PAIR_BUDGET`(Task 3 冻结的 rerank 侧模型
有效窗口)。超预算直接抛异常,不静默截断——passage 组装阶段的截断是"设计内
损失可控的降级",这里如果再截断一次,损失的是打分对象本身的语义完整性,对
判据引擎的可信度是致命的,宁可停工也不能悄悄喂半张卡进 cross-encoder。

**缓存 fail-closed 纪律(P1-9/P1-11)**:`ScoreCache` key = (signal, spec_sha,
variant, qid, canonical_card_id);meta 头记录数据/模型/代码全链路指纹。任一
字段 unknown,或与磁盘上持久化的 meta 不符,**整批拒用**(不是逐条 miss——是
把上一份缓存当作完全不存在,强制全部重算)。只缓存"干净"结果:调用方必须
只在打分函数成功返回后才 `put()`;失败(HTTP 错误 / 解析失败)一律让异常原样
向上传播,绝不能对失败结果调 `put()`(tombstone 纪律——`cached_call()` 这个
薄封装把这条纪律做成了默认行为,调用方不需要自己记得)。

**known-control(P5)**:管线正确性(阻断)与模型质量(非阻断诊断)分离。选卡
规则、阻断断言、诊断项的具体依赖全部走参数注入(gold/candidates/打分函数),
供 Task 6 的 runner 在拿到真实数据与真实打分函数后调用——本任务只测选卡逻辑
与阻断/非阻断分支的正确性,不接触真实分数(live 版本移到 Task 6 Step 5)。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from everos_eval.probe_passage import PAIR_BUDGET, rerank_tokenizer


# ======================================================================
# HTTP 客户端基础设施
# ======================================================================

def _post_json(url: str, payload: dict, *, timeout: int = 60) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:  # 报错先看 body(M1b 铁律,同 retrieve.py:search 的既有约定)
        raise RuntimeError(f"POST {url} HTTP {e.code}: {e.read().decode()[:2000]}") from e


def _assert_index_closure(items: list[dict], n: int, *, index_key: str, label: str) -> dict[int, dict]:
    """P0-2 响应还原硬契约:items 里 index_key 字段的集合必须严格 == {0..n-1}
    (缺失 / 重复 / 越界一律拒)。返回 index → item 的映射供调用方按序取值。"""
    by_index: dict[int, dict] = {}
    for item in items:
        idx = item.get(index_key)
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0 or idx >= n:
            raise ValueError(f"{label}: index 越界或非法: {idx!r}(n={n})")
        if idx in by_index:
            raise ValueError(f"{label}: index 重复: {idx}")
        by_index[idx] = item
    expected = set(range(n))
    if set(by_index) != expected:
        missing = sorted(expected - set(by_index))
        raise ValueError(f"{label}: index 集合不完整,缺失: {missing}")
    return by_index


def _assert_finite(value: Any, *, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{label}: 分数非数值: {value!r}")
    fvalue = float(value)
    if math.isnan(fvalue) or math.isinf(fvalue):
        raise ValueError(f"{label}: 分数非有限值: {value!r}")
    return fvalue


# ======================================================================
# embed() / rerank() / cosine()
# ======================================================================

def embed(texts: list[str], *, base_url: str, model: str, timeout: int = 60) -> list[list[float]]:
    """POST {base_url}/embeddings。响应按 `data[].index` 还原(P0-2 契约);
    校验:index 闭合、分量非有限值一律拒、批内维度全部一致、数量与输入一致。"""
    if not texts:
        return []
    body = _post_json(f"{base_url}/embeddings", {"input": texts, "model": model}, timeout=timeout)
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError(f"embeddings 响应缺 data 数组: {body!r}")
    by_index = _assert_index_closure(data, len(texts), index_key="index", label="embeddings")

    vectors: list[list[float]] = []
    dim: int | None = None
    for i in range(len(texts)):
        vec = by_index[i].get("embedding")
        if not isinstance(vec, list) or not vec:
            raise ValueError(f"embeddings[{i}]: embedding 字段缺失或为空")
        clean_vec = [_assert_finite(x, label=f"embeddings[{i}] 分量") for x in vec]
        if dim is None:
            dim = len(clean_vec)
        elif len(clean_vec) != dim:
            raise ValueError(
                f"embeddings 维度不一致: index {i} 维度={len(clean_vec)}, 期望={dim}(批首维度)"
            )
        vectors.append(clean_vec)
    return vectors


def rerank(query: str, docs: list[str], *, base_url: str, model: str, timeout: int = 60,
           tokenizer=None) -> list[float]:
    """POST {base_url}/rerank。`results` 按分数降序返回,必须按 `item.index`
    scatter 回输入序(P0-2 核心契约——不是边角用例)。

    发请求前先做 CE 对预算断言(Task 3 遗留点名):逐 (query, doc) 对用 Task 3
    的 rerank tokenizer 实测 token 总数,超 `probe_passage.PAIR_BUDGET` 直接
    抛异常,不发请求、不静默截断。
    """
    if not docs:
        return []
    tk = tokenizer if tokenizer is not None else rerank_tokenizer()
    for i, doc in enumerate(docs):
        pair_len = len(tk.encode(query, doc, add_special_tokens=True))
        if pair_len > PAIR_BUDGET:
            raise ValueError(
                f"rerank pair[{i}]: token 总数 {pair_len} 超预算 PAIR_BUDGET={PAIR_BUDGET}"
                "(query+doc 对超出 rerank 模型有效窗口,拒绝静默截断,停工重新审视上游 passage 组装)"
            )

    body = _post_json(f"{base_url}/rerank", {"query": query, "documents": docs, "model": model},
                       timeout=timeout)
    results = body.get("results")
    if not isinstance(results, list):
        raise ValueError(f"rerank 响应缺 results 数组: {body!r}")
    by_index = _assert_index_closure(results, len(docs), index_key="index", label="rerank")

    scores: list[float] = []
    for i in range(len(docs)):
        raw_score = by_index[i].get("relevance_score")
        scores.append(_assert_finite(raw_score, label=f"rerank[{i}] relevance_score"))
    return scores


def cosine(a: list[float], b: list[float]) -> float:
    """标准余弦相似度。维度不一致 / 任一为零向量一律拒(零向量下相似度未定义,
    静默返回 0 会把"打分函数坏了"和"真实语义正交"混为一谈)。"""
    if len(a) != len(b):
        raise ValueError(f"cosine: 维度不一致 {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        raise ValueError("cosine: 零向量无法定义余弦相似度")
    return dot / (norm_a * norm_b)


# ======================================================================
# ScoreCache:key=(signal, spec_sha, variant, qid, canonical_card_id)
# ======================================================================

CACHE_META_FIELDS: tuple[str, ...] = (
    "manifest_sha",
    "embed_model",
    "embed_model_revision",
    "rerank_model",
    "rerank_model_revision",
    "tokenizer_artifact_sha",
    "embedding_dim",
    "cap",
    "pair_budget",
    "passage_spec_sha",
    "decoy_sha",
    "code_git_sha",
    "uv_lock_sha",
)

_UNKNOWN_SENTINELS = (None, "", "unknown")


def _validate_meta(meta: dict) -> None:
    missing = [f for f in CACHE_META_FIELDS if f not in meta]
    if missing:
        raise ValueError(f"cache meta 缺字段: {missing}")
    unknown = [f for f in CACHE_META_FIELDS if meta[f] in _UNKNOWN_SENTINELS]
    if unknown:
        raise ValueError(f"cache meta 含 unknown 值(整批拒用): {unknown}")


class ScoreCache:
    """打分缓存,key = (signal, spec_sha, variant, qid, canonical_card_id)。

    fail-closed 纪律(P1-9/P1-11):构造时当前 run 的 meta 必须全部字段已知
    (unknown/缺字段直接抛异常——这是配置层面的 bug,不是"缓存可能失效",必须
    在源头拦下);若绑定了持久化 `path` 且文件已存在,加载时把磁盘上的 meta
    与当前 meta 整体比较——不符或磁盘 meta 本身含 unknown 值,**整批拒用**
    (`rejected=True`,内存 store 保持空,后续全部当 miss,强制重算)。

    只缓存"干净"结果:调用方应通过 `cached_call()` 使用本类,避免自己在打分
    函数失败路径上误调 `put()`。
    """

    def __init__(self, meta: dict, path: Path | str | None = None):
        _validate_meta(meta)
        self._meta = dict(meta)
        self._path = Path(path) if path is not None else None
        self._store: dict[tuple, Any] = {}
        self._rejected = False
        if self._path is not None and self._path.exists():
            self._load()

    @property
    def rejected(self) -> bool:
        """上次 load() 是否因 meta 不符 / 磁盘 meta 含 unknown 值而整批拒用
        (供调用方在 report 里记账,不是异常——拒用后本对象仍可正常使用,只是
        以空缓存起步)。"""
        return self._rejected

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._rejected = True
            return  # 缓存文件彻底损坏——fail-closed 整批拒用(同 meta 不符路径),不让 run 崩溃
        if not isinstance(raw, dict):
            self._rejected = True
            return  # 合法 JSON 但不是期望的顶层 dict 结构——同样按损坏处理
        stored_meta = raw.get("meta", {})
        try:
            _validate_meta(stored_meta)
        except ValueError:
            self._rejected = True
            return  # 磁盘上的旧缓存自己 meta 就不完整/unknown——整批当不存在
        if stored_meta != self._meta:
            self._rejected = True
            return  # meta 漂移(模型/代码/数据任一变了)——整批当不存在
        self._store = {
            tuple(json.loads(k)): v for k, v in raw.get("entries", {}).items()
        }

    def get(self, signal: str, spec_sha: str, variant: str, qid: str,
            canonical_card_id: str) -> Any:
        key = (signal, spec_sha, variant, qid, canonical_card_id)
        return self._store.get(key)

    def put(self, signal: str, spec_sha: str, variant: str, qid: str,
            canonical_card_id: str, value: Any) -> None:
        key = (signal, spec_sha, variant, qid, canonical_card_id)
        self._store[key] = value

    def save(self) -> None:
        if self._path is None:
            raise ValueError("ScoreCache 未绑定 path,无法 save()")
        entries = {json.dumps(list(k)): v for k, v in self._store.items()}
        self._path.write_text(
            json.dumps({"meta": self._meta, "entries": entries}, ensure_ascii=False),
            encoding="utf-8",
        )


def cached_call(cache: ScoreCache, *, signal: str, spec_sha: str, variant: str, qid: str,
                 canonical_card_id: str, compute_fn: Callable[[], Any]) -> Any:
    """缓存优先的薄封装:命中直接返回,**不调用 `compute_fn`**(缓存命中不发
    请求,这是 P1-9 缓存纪律的核心断言之一)。未命中才调 `compute_fn()`;
    `compute_fn` 抛出的异常原样向上传播、绝不落缓存——这就是 tombstone 纪律:
    失败结果不会被误当成"已尝试过的空结果"缓存下来,下次调用仍会真实重算。
    """
    hit = cache.get(signal, spec_sha, variant, qid, canonical_card_id)
    if hit is not None:
        return hit
    value = compute_fn()  # 失败在此直接向上抛,不执行下面的 put()
    cache.put(signal, spec_sha, variant, qid, canonical_card_id, value)
    return value


# ======================================================================
# known-control(P5):选卡 + 阻断断言(管线正确性)+ 非阻断诊断(模型质量)
# ======================================================================

# 单条 vs batch rerank 一致性容差(仪器常数,2026-07-16 真数据 phase1 实测校准)。
# 现象:初版 1e-4 容差被真实 Infinity 打分拦下(单条 vs batch 差 0.0049)。
# 控制面仪器诊断三证据:①换序对照证明 scatter 正确——batch 正序/逆序对齐后分数
# 逐位一致(差 0.000000),分数跟卡走,不是 index 错配;②相关/无关卡分离清晰
# (0.64 vs 1e-4 量级);③单条 vs batch 实测 max 差 0.0049,是批组成数值效应
# (cross-encoder padding/kernel 所致),良性。
# 取值:实测良性批漂移上界 ~5e-3 的 4 倍 = 0.02。真正的 scatter 错位是数量级
# 错配(把别的卡的分数配错过来,差值在 0.1~1 量级),0.02 仍必抓。
# 预注册视角:known-control 规则本身未变,变的是实现层仪器常数,且校准发生在
# 任何臂分数产生之前(phase2 之前)。
BATCH_CONSISTENCY_TOLERANCE = 0.02

@dataclass(frozen=True)
class KnownControlSelection:
    q_star: str
    relevant: dict                  # gold-relevant ∧ useful,字典序最小
    same_type_irrelevant: dict      # 同型 gold-irrelevant,字典序最小


@dataclass(frozen=True)
class KnownControlResult:
    selection: KnownControlSelection
    warnings: list[str]


def select_known_control_cards(gold: dict, candidates_by_qid: dict[str, list[dict]]
                                 ) -> KnownControlSelection:
    """P5 选卡规则(冻结 plan §P5 正文,两卡版):q* = primary gold 编号最小的
    covered 查询;卡只从 q* 自己的 33 个冻结候选中选——字典序最小的
    gold-relevant∧useful 候选 + 同型字典序最小的 gold-irrelevant 候选。

    R1 曾从全语料选卡,实测选出的卡不在候选池、无 payload/native 分,必卡死
    ——本规则(R5)把选卡范围收紧到 q* 自己的候选池,保证选出的卡一定带着
    真实 payload 与 native 分。
    """
    primary = gold["primary"]
    covered = primary["covered"]
    if not covered:
        raise ValueError("known-control: primary gold 无 covered 查询,无法选 q*")
    q_star = min(covered)

    candidates = candidates_by_qid.get(q_star)
    if not candidates:
        raise ValueError(f"known-control: q*={q_star!r} 在 candidates_by_qid 中无候选")

    labels = primary["labels"]

    def _label(cid: str) -> dict:
        return labels.get((q_star, cid), {})

    ranked = sorted(candidates, key=lambda c: c["canonical_card_id"])

    relevant_pool = [
        c for c in ranked
        if _label(c["canonical_card_id"]).get("relevant") and _label(c["canonical_card_id"]).get("useful")
    ]
    if not relevant_pool:
        raise ValueError(f"known-control: q*={q_star!r} 无 gold-relevant∧useful 候选,选卡规则卡死")
    relevant = relevant_pool[0]
    rel_type = relevant["mem_type"]
    rel_cid = relevant["canonical_card_id"]

    def _is_gold_irrelevant(c: dict) -> bool:
        return _label(c["canonical_card_id"]).get("relevant") is False

    same_type_pool = [
        c for c in ranked
        if c["mem_type"] == rel_type and c["canonical_card_id"] != rel_cid and _is_gold_irrelevant(c)
    ]
    if not same_type_pool:
        raise ValueError(f"known-control: q*={q_star!r} 无同型 gold-irrelevant 候选,选卡规则卡死")
    same_type_irrelevant = same_type_pool[0]

    return KnownControlSelection(
        q_star=q_star,
        relevant=relevant,
        same_type_irrelevant=same_type_irrelevant,
    )


def run_known_control_checks(
    selection: KnownControlSelection,
    *,
    query_text: str,
    passages: list[str],  # 与 [relevant, same_type_irrelevant] 对齐
    cards_ids: set,
    gold_ids: set,
    rerank_fn: Callable[[str, list[str]], list[float]],
    embed_fn: Callable[[list[str]], list[list[float]]],
    expected_native_scores: dict,
    tolerance: float = BATCH_CONSISTENCY_TOLERANCE,
) -> KnownControlResult:
    """P5 known-control 执行(真实调用由 Task 6 runner 注入 rerank_fn/embed_fn,
    本任务只测下面这套判断逻辑本身,不接触真实分数)。

    **阻断断言(管线正确性,失败即 AssertionError,Task 6 brief:"不过 → 停,
    修管线,不进 Task 5")**:
    1. canonical 闭合:两张卡都在 cards_ids 与 gold_ids 内;
    2. native 分与冻结台账逐位相等;
    3. rerank 单条调用 vs batch 调用分数一致(index scatter 对照——如果客户端
       自己的 index 还原逻辑有 bug,这里会先于其它任何指标暴露出来)。

    **非阻断诊断(模型质量,不影响管线判定,只记 warning)**:
    相关卡的 cos 与 ce 分数是否高于**同型**无关卡(native 分跨类型不可比,
    不参与此项诊断)。
    """
    cards = [selection.relevant, selection.same_type_irrelevant]

    # ---- 阻断 1:canonical 闭合 ----
    for c in cards:
        cid = c["canonical_card_id"]
        if cid not in cards_ids:
            raise AssertionError(f"known-control 阻断:{cid} 不在 cards.jsonl id 集(canonical 闭合失败)")
        if cid not in gold_ids:
            raise AssertionError(f"known-control 阻断:{cid} 不在 gold card_id 集(canonical 闭合失败)")

    # ---- 阻断 2:native 分与冻结台账逐位相等 ----
    for c in cards:
        cid = c["canonical_card_id"]
        if cid not in expected_native_scores:
            raise AssertionError(f"known-control 阻断:{cid} 缺台账 native 分记录(逐位核对失败)")
        if c["native_score"] != expected_native_scores[cid]:
            raise AssertionError(
                f"known-control 阻断:{cid} native 分与台账不等"
                f"(candidate={c['native_score']!r}, 台账={expected_native_scores[cid]!r})"
            )

    # ---- 阻断 3:rerank 单条 vs batch 调用分数一致(index scatter 对照) ----
    batch_scores = rerank_fn(query_text, passages)
    if len(batch_scores) != len(passages):
        raise AssertionError(
            f"known-control 阻断:batch rerank 返回数量({len(batch_scores)})与候选数({len(passages)})不符"
        )
    for i, p in enumerate(passages):
        single_scores = rerank_fn(query_text, [p])
        if len(single_scores) != 1:
            raise AssertionError(f"known-control 阻断:单条 rerank[{i}] 返回数量异常: {single_scores!r}")
        if abs(single_scores[0] - batch_scores[i]) > tolerance:
            raise AssertionError(
                "known-control 阻断:单条 vs batch rerank 分数不一致(index scatter 对照失败): "
                f"single={single_scores[0]!r} batch[{i}]={batch_scores[i]!r}"
            )

    # ---- 非阻断诊断:同型 cos / ce 序关系(相关卡应高于同型无关卡) ----
    warnings: list[str] = []
    ce_relevant, ce_same_irrelevant = batch_scores[0], batch_scores[1]
    if not (ce_relevant > ce_same_irrelevant):
        warnings.append(
            "known-control 非阻断诊断:同型 ce 序反了"
            f"(ce_relevant={ce_relevant!r} <= ce_same_type_irrelevant={ce_same_irrelevant!r})"
        )

    vectors = embed_fn([query_text, passages[0], passages[1]])
    q_vec, rel_vec, same_irr_vec = vectors
    cos_relevant = cosine(q_vec, rel_vec)
    cos_same_irrelevant = cosine(q_vec, same_irr_vec)
    if not (cos_relevant > cos_same_irrelevant):
        warnings.append(
            "known-control 非阻断诊断:同型 cos 序反了"
            f"(cos_relevant={cos_relevant!r} <= cos_same_type_irrelevant={cos_same_irrelevant!r})"
        )

    return KnownControlResult(selection=selection, warnings=warnings)
