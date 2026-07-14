from everos_eval.retrieve import merge_top5


def _it(i, s=None):
    return {"id": i, "score": s}


def test_merge_interleave_skill_first():
    cases = [_it("c1", 0.9), _it("c2", 0.5), _it("c3", 0.4)]
    skills = [_it("s1", 0.1), _it("s2", 0.05)]
    top = merge_top5(cases, skills)
    # 交错取 5:跨类型分数不可比(RRF vs cross-encoder),分数不参与排序(spec R5)
    assert [t["id"] for t in top] == ["s1", "c1", "s2", "c2", "c3"]
    assert top[0]["mem_type"] == "agent_skill" and top[1]["mem_type"] == "agent_case"


def test_merge_fills_from_other_side_when_exhausted():
    top = merge_top5([_it("c1")], [_it(f"s{i}") for i in range(1, 6)])
    assert [t["id"] for t in top] == ["s1", "c1", "s2", "s3", "s4"]


def test_merge_uses_array_order_not_score():
    cases = [_it("c1", 0.1), _it("c2", 0.99)]  # 数组顺序即类型内排名,分数刻意反着放
    skills = [_it("s1", 0.2)]
    assert [t["id"] for t in merge_top5(cases, skills)] == ["s1", "c1", "c2"]
