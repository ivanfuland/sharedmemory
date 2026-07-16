"""检索台账候选加载器(P0-1 修法):把 raw_response 里的候选卡 id 归一到 gold 标签空间。

背景(上一轮评审抓出的 P0):/memory/search 返回的 agent_case id 带 agent 前缀
(everos-m1b-probe_ac_...),而 cards.jsonl / L1 judge 台账用的是无前缀 canonical id
(ac_...)。若直接拿 search 返回 id 去跟 gold 标签求交集,交集恒为 0——离线实验会
产出假指标(全部候选"未命中")。本模块负责在候选加载这一步就把 id 归一好,并用
硬断言强制这条闭合关系,不归一直接崩,不让坏数据流入后续统计。
"""
from __future__ import annotations

from everos_eval.retrieve import canonical_id


def load_candidates(retrieval_row: dict) -> list[dict]:
    """把一行 retrieval.jsonl 的 raw_response.agent_cases / agent_skills 转成候选列表。

    每元素:
    - canonical_card_id: 去前缀后的 id(调 everos_eval.retrieve.canonical_id 归一)
    - mem_type: "agent_case" | "agent_skill"
    - source_rank: 该卡在其类型数组里的原始下标(0 起)
    - native_score: 该元素的 score 字段
    - payload: 原始元素 dict(未改动)

    硬断言(P0-1):该行候选总数恰为 33(20 case + 13 skill,真实数据实测不变量);
    canonical id 行内唯一。任一违反直接 AssertionError——这是台账数据完整性的
    前置校验,不是可选项。
    """
    raw = retrieval_row["raw_response"]
    cases = raw["agent_cases"]
    skills = raw["agent_skills"]

    candidates: list[dict] = []
    for rank, item in enumerate(cases):
        candidates.append({
            "canonical_card_id": canonical_id(item["id"], "agent_case"),
            "mem_type": "agent_case",
            "source_rank": rank,
            "native_score": item["score"],
            "payload": item,
        })
    for rank, item in enumerate(skills):
        candidates.append({
            "canonical_card_id": canonical_id(item["id"], "agent_skill"),
            "mem_type": "agent_skill",
            "source_rank": rank,
            "native_score": item["score"],
            "payload": item,
        })

    query_id = retrieval_row.get("query_id")
    assert len(candidates) == 33, (
        f"query_id={query_id!r}: expected exactly 33 candidates (20 case + 13 skill), "
        f"got {len(candidates)}"
    )

    seen: set[str] = set()
    for c in candidates:
        cid = c["canonical_card_id"]
        assert cid not in seen, (
            f"query_id={query_id!r}: duplicate canonical_card_id {cid!r} within row"
        )
        seen.add(cid)

    return candidates


def assert_closure(candidates: list[dict], cards_ids: set, gold_ids: set) -> None:
    """闭合断言(供 runner 逐行调用):每个候选的 canonical_card_id 必须同时落在
    cards.jsonl id 集与 L1 gold card_id 集内。

    case id 若未归一(仍带 agent 前缀),与两个集合的交集皆为 0,这里必炸——
    这正是本探针要测的东西(P0-1 的核心回归门)。
    """
    for c in candidates:
        cid = c["canonical_card_id"]
        assert cid in cards_ids, f"canonical_card_id {cid!r} not in cards.jsonl id set"
        assert cid in gold_ids, f"canonical_card_id {cid!r} not in L1 gold card_id set"
