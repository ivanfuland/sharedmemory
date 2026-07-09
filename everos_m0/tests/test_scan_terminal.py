"""markdown 终态扫描。

为什么终态判定要扫 markdown 而不是打 `/search`：`SearchRequest` 强制非空 `query`，
且 agent 搜索固定 case+skill 一起、`agent_skill` 根本没有 `session_id` —— 它不是
「列出本 session 产物」的 API。而 markdown 是 EverOS 的 source-of-truth 公开契约
（非内部 DB schema），黑盒友好、cheap fs。

SAMPLE 摘自 M0 真实产出的 agent_case-2026-07-09.md，非臆造格式。
"""

from everos_m0.scan_terminal import (
    entry_ids_in,
    session_case_entry_ids,
    session_has_case,
    session_ids_in,
)

# 真实产出片段（agents/ivan-coding/.cases/agent_case-2026-07-09.md）
SAMPLE = """---
id: agent_case_log_ivan-coding_2026-07-09
type: agent_case_daily
agent_id: ivan-coding
entry_count: 2
---
<!-- entry:ac_20260709_00000001 -->
## ac_20260709_00000001

**owner_id**: ivan-coding
**session_id**: m0-synth-0001
**timestamp**: 2025-07-08T08:00:02.300000+00:00
**parent_type**: memcell
**parent_id**: mc_4c13421e9c6c
**quality_score**: 1.0

### TaskIntent
修复 pytest 中 test_foo 的失败

### KeyInsight
单个测试失败时，先跑全量测试验证改动影响。
<!-- /entry:ac_20260709_00000001 -->
<!-- entry:ac_20260709_00000002 -->
## ac_20260709_00000002

**owner_id**: ivan-coding
**session_id**: other-session
**quality_score**: 0.8
<!-- /entry:ac_20260709_00000002 -->
"""


def test_session_ids_parsed():
    assert set(session_ids_in(SAMPLE)) == {"m0-synth-0001", "other-session"}


def test_session_has_case():
    assert session_has_case(SAMPLE, "m0-synth-0001") is True
    assert session_has_case(SAMPLE, "nope") is False


def test_entry_ids_parsed():
    # 开标记和闭标记不能重复计数
    assert entry_ids_in(SAMPLE) == ["ac_20260709_00000001", "ac_20260709_00000002"]


def test_case_entry_id_bound_to_its_own_session():
    # M1 的 A->B 溯源桥依赖这个映射；串了就把攻略指向错的原始会话
    assert session_case_entry_ids(SAMPLE, "m0-synth-0001") == ["ac_20260709_00000001"]
    assert session_case_entry_ids(SAMPLE, "other-session") == ["ac_20260709_00000002"]


def test_substring_session_id_not_matched():
    # "m0-synth-000" 是 "m0-synth-0001" 的前缀，不能误判为命中
    assert session_has_case(SAMPLE, "m0-synth-000") is False


def test_empty_text():
    assert session_ids_in("") == [] and entry_ids_in("") == []
