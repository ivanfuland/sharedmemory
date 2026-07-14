"""EverOS /memory/search 客户端 + 预注册 top-5 合并(spec R4 §10)。"""
from __future__ import annotations
import json
import urllib.request


def merge_top5(agent_cases, agent_skills, k: int = 5):
    """确定性交错(spec R5):skill/case 各按数组内部排名,skill 先,一侧耗尽另一侧补齐。
    跨类型分数不可比(case=RRF fusion 分,skill=cross-encoder 分),score 只记账不排序。"""
    def _wrap(items, mem_type):
        return [{"id": it["id"], "mem_type": mem_type, "score": it.get("score"), "payload": it}
                for it in items]
    sk, ca = _wrap(agent_skills, "agent_skill"), _wrap(agent_cases, "agent_case")
    out, i = [], 0
    while len(out) < k and (i < len(sk) or i < len(ca)):
        if i < len(sk):
            out.append(sk[i])
        if len(out) < k and i < len(ca):
            out.append(ca[i])
        i += 1
    return out


def search(base_url: str, agent_id: str, query: str, top_k: int = 20) -> dict:
    body = json.dumps({"agent_id": agent_id, "query": query, "method": "hybrid",
                       "top_k": top_k, "enable_llm_rerank": False}).encode()
    req = urllib.request.Request(f"{base_url}/api/v1/memory/search", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:  # 报错先看 body(M1b 铁律)
        raise RuntimeError(f"search HTTP {e.code}: {e.read().decode()[:2000]}") from e
