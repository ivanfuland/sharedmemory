# tests/test_m4_distiller_validate.py — _validate 候选粒度过滤（codex 审 R）
import pytest
from distill import distiller

_OK = {"entity_name": "X", "entity_kind": "project", "entry_type": "fact",
       "fact_text": "F", "source_idx": 0}

def test_drops_bad_keeps_good():
    bad_enum = {**_OK, "entity_kind": "Project"}        # 枚举大小写抖动
    bad_float = {**_OK, "source_idx": 1.0}              # float 而非 int
    extra = {**_OK, "confidence": 0.9}                  # 多字段
    out = distiller._validate({"candidates": [_OK, bad_enum, bad_float, extra, dict(_OK)]})
    assert len(out["candidates"]) == 2                  # 只留两个合法的，不废整 span

def test_all_bad_raises():                              # 全坏 → quarantine
    with pytest.raises(AssertionError):
        distiller._validate({"candidates": [{**_OK, "entity_kind": "nope"}]})

def test_empty_ok():                                   # 合法空数组（无可记）→ 放行
    assert distiller._validate({"candidates": []}) == {"candidates": []}

def test_top_level_malformed_raises():                 # 顶层坏 → 抛
    with pytest.raises(AssertionError):
        distiller._validate({"items": []})
    with pytest.raises(AssertionError):
        distiller._validate({"candidates": "notalist"})
