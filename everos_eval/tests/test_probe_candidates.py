"""probe_candidates.py 的测试(P0-1 修法:候选卡 id 归一到 gold 标签空间)。

fixture 用真实前缀形态复刻 2026-07-14 sanity 实证的观察:
- agent_case 的 search 返回 id 带 agent 前缀(everos-m1b-probe_ac_...),
  cards.jsonl / L1 gold 侧的 canonical id 无前缀(ac_...)。
- agent_skill 的 canonical frontmatter id 本就含前缀,两边一致,原样通过。
真实数据里每行恰好 20 agent_case + 13 agent_skill = 33 条(见 retrieval.jsonl 60 行实测),
故下面的 happy-path fixture 也构造成 20+13,以便同时覆盖「恰 33 条」这条硬断言。
"""
import json
import os
from pathlib import Path

import pytest

from everos_eval.probe_candidates import assert_closure, load_candidates

DATA_DIR_ENV = "EVEROS_PROBE2B_DATA"

# 20 个 case id(真实前缀形态),对应的 gold/cards 侧应去前缀
CASE_IDS_RAW = [f"everos-m1b-probe_ac_20260713_{i:08d}" for i in range(1, 21)]
CASE_IDS_GOLD = [cid[len("everos-m1b-probe_"):] for cid in CASE_IDS_RAW]

# 13 个 skill id(真实前缀形态),gold/cards 侧原样保留前缀
SKILL_IDS = [f"everos-m1b-probe_技能{i}" for i in range(1, 14)]


def _row(case_ids=None, skill_ids=None, query_id="q01"):
    case_ids = CASE_IDS_RAW if case_ids is None else case_ids
    skill_ids = SKILL_IDS if skill_ids is None else skill_ids
    cases = [{"id": cid, "agent_id": "everos-m1b-probe", "score": 0.9 - i * 0.01}
             for i, cid in enumerate(case_ids)]
    skills = [{"id": sid, "name": sid, "score": 0.5 - i * 0.01}
              for i, sid in enumerate(skill_ids)]
    return {
        "query_id": query_id,
        "raw_response": {"agent_cases": cases, "agent_skills": skills},
    }


# ---- load_candidates: canonical 归一 + 字段保留 ----

def test_load_candidates_strips_agent_prefix_from_case_ids():
    candidates = load_candidates(_row())
    case_candidates = [c for c in candidates if c["mem_type"] == "agent_case"]
    assert len(case_candidates) == 20
    assert case_candidates[0]["canonical_card_id"] == "ac_20260713_00000001"
    assert all(not c["canonical_card_id"].startswith("everos-m1b-probe_") for c in case_candidates)


def test_load_candidates_keeps_skill_ids_as_is():
    candidates = load_candidates(_row())
    skill_candidates = [c for c in candidates if c["mem_type"] == "agent_skill"]
    assert len(skill_candidates) == 13
    assert skill_candidates[0]["canonical_card_id"] == "everos-m1b-probe_技能1"


def test_load_candidates_preserves_source_rank_within_type():
    candidates = load_candidates(_row())
    case_candidates = [c for c in candidates if c["mem_type"] == "agent_case"]
    skill_candidates = [c for c in candidates if c["mem_type"] == "agent_skill"]
    assert [c["source_rank"] for c in case_candidates] == list(range(20))
    assert [c["source_rank"] for c in skill_candidates] == list(range(13))


def test_load_candidates_preserves_native_score():
    candidates = load_candidates(_row())
    case_candidates = [c for c in candidates if c["mem_type"] == "agent_case"]
    assert case_candidates[3]["native_score"] == pytest.approx(0.9 - 3 * 0.01)


def test_load_candidates_preserves_payload():
    row = _row()
    candidates = load_candidates(row)
    case0 = next(c for c in candidates if c["mem_type"] == "agent_case" and c["source_rank"] == 0)
    assert case0["payload"] == row["raw_response"]["agent_cases"][0]


def test_load_candidates_returns_33_total():
    assert len(load_candidates(_row())) == 33


# ---- load_candidates: 硬断言 ----

def test_load_candidates_rejects_wrong_total_count():
    row = _row(case_ids=CASE_IDS_RAW[:19])  # 19 case + 13 skill = 32,非 33
    with pytest.raises(AssertionError):
        load_candidates(row)


def test_load_candidates_rejects_duplicate_canonical_id_within_row():
    dup_case_ids = CASE_IDS_RAW[:19] + [CASE_IDS_RAW[0]]  # 仍是 20 个,但首尾重复
    row = _row(case_ids=dup_case_ids)
    with pytest.raises(AssertionError):
        load_candidates(row)


# ---- assert_closure: 闭合断言 ----

def _gold_and_cards_ids():
    ids = set(CASE_IDS_GOLD) | set(SKILL_IDS)
    return ids, ids


def test_assert_closure_passes_when_all_ids_normalized():
    candidates = load_candidates(_row())
    cards_ids, gold_ids = _gold_and_cards_ids()
    assert_closure(candidates, cards_ids, gold_ids)  # 不应抛异常


def test_assert_closure_raises_when_case_id_not_normalized():
    # 复现 P0-1:若 case id 未去前缀,canonical_card_id 与 gold/cards 集合交集为 0
    candidates = load_candidates(_row())
    cards_ids, gold_ids = _gold_and_cards_ids()
    candidates[0]["canonical_card_id"] = "everos-m1b-probe_" + candidates[0]["canonical_card_id"]
    with pytest.raises(AssertionError):
        assert_closure(candidates, cards_ids, gold_ids)


def test_assert_closure_raises_when_id_not_in_gold_set():
    candidates = load_candidates(_row())
    cards_ids, _ = _gold_and_cards_ids()
    gold_ids = cards_ids - {CASE_IDS_GOLD[0]}  # 制造「不在 gold」场景
    with pytest.raises(AssertionError):
        assert_closure(candidates, cards_ids, gold_ids)


def test_assert_closure_raises_when_id_not_in_cards_set():
    candidates = load_candidates(_row())
    _, gold_ids = _gold_and_cards_ids()
    cards_ids = gold_ids - {SKILL_IDS[0]}  # 制造「不在 cards」场景
    with pytest.raises(AssertionError):
        assert_closure(candidates, cards_ids, gold_ids)


# ---- Step 4: 真数据 60/60 闭合断言(可选 live 测试,需显式给数据目录) ----

def _real_data_dir():
    raw = os.environ.get(DATA_DIR_ENV)
    return Path(raw) if raw else None


@pytest.mark.live
def test_real_data_60_of_60_closure():
    data_dir = _real_data_dir()
    if data_dir is None or not data_dir.exists():
        pytest.skip(f"set {DATA_DIR_ENV}=<probe-2b data dir> to run this live test")

    cards_ids = set()
    with (data_dir / "cards.jsonl").open(encoding="utf-8") as f:
        for line in f:
            cards_ids.add(json.loads(line)["card_id"])

    gold_ids = set()
    with (data_dir / "l1_verdicts.jsonl").open(encoding="utf-8") as f:
        for line in f:
            job_id = json.loads(line)["job_id"]
            gold_ids.add(job_id.split(":", 2)[2])  # l1:qXX:<card_id> 第三段

    total = 0
    passed = 0
    with (data_dir / "retrieval.jsonl").open(encoding="utf-8") as f:
        for line in f:
            total += 1
            row = json.loads(line)
            candidates = load_candidates(row)
            assert_closure(candidates, cards_ids, gold_ids)
            passed += 1

    print(f"\n真数据闭合断言:{passed}/{total} 通过")
    assert total == 60
    assert passed == total
