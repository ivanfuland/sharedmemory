# everos_mcp/upstream.py
"""EverOS 上游客户端——唯一经 `everos_mcp.http.post_json` 出站。

请求体逐字段对齐既有 `everos_eval/retrieve.py::search`(agent_id/query/
method="hybrid"/top_k=20/enable_llm_rerank=False),但走 http.py 的 loopback
断言 + redirect 拒绝 + BadJson 契约(这是本任务修复的"出站唯一通道",
retrieve.py 保持不动,不复用它的 urlopen 调用)。

真实响应 envelope(EverOS dto + retrieve.py/scripts/eval_run_m1c.py 双证):
`{request_id, data:{agent_cases, agent_skills, ...}}`。`normalize_candidates`
校验:两个数组存在、卡 id 非空且跨两个数组无重复、native score 是 finite
float 或 None、**候选 payload 白名单字段 schema 合法**(P1f/P2a,见下);任一
违规一律 `UpstreamBadResponse(code="everos_bad_response")`——绝不落 `internal`。

**payload schema 校验(P1f/P2a)**:此前候选 payload 里的白名单字段
(case=task_intent/approach/key_insight;skill=name/description/content)
完全不校验类型/存在性,两类问题因此能滑过 normalize 阶段:
①一个非 str 值(list/dict/数值)会让 `contract.clamp_payload` 把它的
"长度"算成 0(`len(x) if isinstance(x, str) else 0`),截断上限被静默绕过
(P2a:clamp 类型旁路);②`blobstore.build_snapshots` 调用
`probe_passage.build_passage`(spec 固定 "prod")时,该函数对 prod 规格必需
字段做 `payload[field]` 取值 + `"\\n".join(...)` 拼接——字段缺失原生
`KeyError`、字段为 None 原生 `TypeError`,这两个异常都不在 server.py 的
显式 except 分支里,滑进最外层 broad except 被误判成 `internal`(P1f)。

修法:在候选构造时把 schema 校验前移到这里——白名单字段值必须是 str 或
None(缺失等同 None);prod 规格必需字段(case:task_intent/approach;
skill:name/description,与 `everos_eval.probe_passage._PROD_CASE_FIELDS`/
`_PROD_SKILL_FIELDS` 同源,不在本模块重新硬编码这份"builder 需要什么"的
知识)不许是 None/缺失。可选字段(key_insight/content)允许 None/缺失,但
类型非法(非 str 非 None)仍要拒——"可选"只豁免"没给",不豁免"给了垃圾"。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.error import HTTPError

from everos_eval.probe_passage import _PROD_CASE_FIELDS, _PROD_SKILL_FIELDS
from everos_mcp import http
from everos_mcp.config import Config
from everos_mcp.contract import ContractError

# 与 contract._WHITELIST 的字段名一致(clamp_payload 消费同一份白名单)——
# 这里独立维护一份是因为 upstream.py 不依赖 everos_mcp.contract 的私有字典
# 结构,只需要"这几个字段名"这个事实;必需字段直接从 probe_passage 的 prod
# 规格常量导入,避免"builder 需要哪些字段"这份知识在两处重复维护而漂移。
_WHITELIST_FIELDS = {
    "agent_case": ("task_intent", "approach", "key_insight"),
    "agent_skill": ("name", "description", "content"),
}
_REQUIRED_PASSAGE_FIELDS = {
    "agent_case": _PROD_CASE_FIELDS,
    "agent_skill": _PROD_SKILL_FIELDS,
}

# 与 everos_eval/retrieve.py::search 的既有端点一致(不改动该模块,逐字段复刻)。
_SEARCH_PATH = "/api/v1/memory/search"

# HTTPError body 只读前 2000 字节进异常消息(M1b 铁律)——绝不整体读入内存,
# 也绝不让这段 body 文本流入任何面向用户的 reason 字段(该字段由上层任务构造,
# 本层只保证异常消息里的 body 是截断过的原始诊断信息)。
_HTTP_ERROR_BODY_CAP = 2000


class UpstreamBadResponse(ContractError):
    """EverOS 响应违反契约(信封结构 / 卡 id / native score)。一律 code=everos_bad_response,
    BadJson(JSON 解析失败)与 envelope 校验失败统一走这一个异常类。"""

    def __init__(self, msg: str = "", *, code: str = "everos_bad_response"):
        super().__init__(code, msg)


class UpstreamHTTPError(ContractError):
    """EverOS HTTP 层错误(4xx/5xx)。message 含 body 前 2000 字节,仅供诊断——
    调用方构造用户可见 reason 时不得读取这段 body 文本。"""

    def __init__(self, msg: str = "", *, code: str = "everos_http_error"):
        super().__init__(code, msg)


@dataclass(frozen=True)
class Candidate:
    id: str
    mem_type: str
    native_score: float | None
    payload: dict
    source_rank: int


@dataclass(frozen=True)
class NormalizedSearch:
    everos_request_id: str
    cases: list
    skills: list


def search(cfg: Config, query: str) -> dict:
    """POST EverOS /memory/search,timeout=10,经 http.post_json 出站。"""
    payload = {
        "agent_id": cfg.agent_id,
        "query": query,
        "method": "hybrid",
        "top_k": 20,
        "enable_llm_rerank": False,
    }
    url = f"{cfg.everos_base}{_SEARCH_PATH}"
    try:
        return http.post_json(url, payload, timeout=10)
    except HTTPError as e:
        body = e.read(_HTTP_ERROR_BODY_CAP)
        raise UpstreamHTTPError(f"EverOS search HTTP {e.code}: {body!r}") from e
    except http.BadJson as e:
        raise UpstreamBadResponse(f"EverOS search 响应解析失败: {e}") from e
    except http.ResponseTooLarge as e:
        # P1c:此前只捕获 BadJson,ResponseTooLarge 会滑过本函数、被 server.py
        # 最外层 broad except 误判成 error_code="internal"——这本质上是上游
        # 响应异常(响应体超出可信上限),与 BadJson 同一类"响应不合法"故障,
        # 应同码 everos_bad_response,不落 internal。异常消息本身不回显任何
        # body 内容(`http.ResponseTooLarge` 已经只报字节数,不带内容片段)。
        raise UpstreamBadResponse(f"EverOS search 响应超限: {e}") from e


def _require_array(data: dict, key: str) -> list:
    items = data.get(key)
    if not isinstance(items, list):
        raise UpstreamBadResponse(f"EverOS 响应 data.{key} 缺数组: {data!r}")
    return items


def _finite_or_none(value, *, label: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise UpstreamBadResponse(f"{label}: native score 非数值: {value!r}")
    fvalue = float(value)
    if math.isnan(fvalue) or math.isinf(fvalue):
        raise UpstreamBadResponse(f"{label}: native score 非有限值: {value!r}")
    return fvalue


def _validate_payload_schema(item: dict, mem_type: str, *, label: str) -> None:
    """白名单字段(P1f/P2a):值必须是 str 或 None/缺失;prod 规格必需字段
    (`_REQUIRED_PASSAGE_FIELDS`)不许是 None/缺失——这两条分别对应
    `build_snapshots`(spec 固定 "prod")实际会踩到的两种炸法。"""
    whitelist = _WHITELIST_FIELDS[mem_type]
    required = _REQUIRED_PASSAGE_FIELDS[mem_type]
    for field in whitelist:
        value = item.get(field)
        if value is None:
            if field in required:
                raise UpstreamBadResponse(
                    f"{label}: 缺少必需字段 {field!r}(prod passage 规格需要非 None 值)"
                )
            continue
        if not isinstance(value, str):
            raise UpstreamBadResponse(
                f"{label}: 字段 {field!r} 类型非法(需 str 或 None,实际 {type(value).__name__})"
            )


def _build_candidates(items: list, mem_type: str, seen_ids: set) -> list:
    out = []
    for rank, item in enumerate(items):
        if not isinstance(item, dict):
            raise UpstreamBadResponse(f"{mem_type}[{rank}]: 候选非对象: {item!r}")
        cid = item.get("id")
        if not isinstance(cid, str) or not cid:
            raise UpstreamBadResponse(f"{mem_type}[{rank}]: id 缺失/空/非字符串: {cid!r}")
        if cid in seen_ids:
            raise UpstreamBadResponse(f"{mem_type}[{rank}]: id 重复(跨 agent_cases/agent_skills): {cid!r}")
        seen_ids.add(cid)
        native_score = _finite_or_none(item.get("score"), label=f"{mem_type}[{rank}] id={cid!r}")
        _validate_payload_schema(item, mem_type, label=f"{mem_type}[{rank}] id={cid!r}")
        out.append(Candidate(id=cid, mem_type=mem_type, native_score=native_score,
                              payload=item, source_rank=rank))
    return out


def normalize_candidates(resp: dict) -> NormalizedSearch:
    """解析真实 envelope `{request_id, data:{agent_cases, agent_skills}}`。

    校验顺序:顶层是对象 -> request_id 非空字符串 -> data 是对象 -> 两个数组存在
    -> 逐候选 id 非空且跨两数组无重复、native score finite-or-None。任一违规
    `UpstreamBadResponse(code="everos_bad_response")`。

    `http.post_json` 只保证返回 `json.loads` 的结果——顶层合法 JSON 但非对象
    (数组/字符串/数字/null)必须在这里挡住,不能让 `resp.get(...)` 摔
    AttributeError 滑进外层 broad except 被误判成 internal。
    """
    if not isinstance(resp, dict):
        raise UpstreamBadResponse(f"EverOS 响应顶层非对象: {type(resp).__name__}")

    request_id = resp.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise UpstreamBadResponse(f"EverOS 响应 request_id 缺失/空/非字符串: {request_id!r}")

    data = resp.get("data")
    if not isinstance(data, dict):
        raise UpstreamBadResponse(f"EverOS 响应缺 data 对象: {resp!r}")

    cases_items = _require_array(data, "agent_cases")
    skills_items = _require_array(data, "agent_skills")

    seen_ids: set = set()
    cases = _build_candidates(cases_items, "agent_case", seen_ids)
    skills = _build_candidates(skills_items, "agent_skill", seen_ids)

    return NormalizedSearch(everos_request_id=request_id, cases=cases, skills=skills)
