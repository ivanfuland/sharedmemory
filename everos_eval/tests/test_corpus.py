from pathlib import Path
from everos_eval.corpus import Card, load_skills, load_entries, load_all_cards

SKILL_MD = """---
id: sk_test_0001
type: agent_skill
name: 合成技能甲
confidence: 0.5
---
## Steps
1. 假步骤
"""

CASES_MD = """---
id: agent_case_log_x_2026-01-01
entry_count: 2
---
<!-- entry:ac_20260101_00000001 -->
## ac_20260101_00000001
session_id: syn-1
### TaskIntent
合成任务甲
<!-- entry:ac_20260101_00000002 -->
## ac_20260101_00000002
session_id: syn-2
### TaskIntent
合成任务乙
"""

def _mk_instance(tmp_path: Path) -> Path:
    skills = tmp_path / "default_app/default_project/agents/everos-m1b-probe/skills"
    (skills / "skill_甲").mkdir(parents=True)
    (skills / "skill_甲" / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    nested = skills / "skill_X" / "Nested skill"
    nested.mkdir(parents=True)
    # 嵌套卡同时复刻真实 id 形态:含空格/中文/全角冒号(真实 id 如 "everos-m1b-probe_TDD 基础件实现：先写完整测试再实现")
    (nested / "SKILL.md").write_text(SKILL_MD.replace("sk_test_0001", "everos-test_嵌套 技能：含空格 id").replace("甲", "乙"), encoding="utf-8")
    cases = tmp_path / "default_app/default_project/agents/everos-m1b-probe/.cases"
    cases.mkdir(parents=True)
    (cases / "agent_case-2026-01-01.md").write_text(CASES_MD, encoding="utf-8")
    return tmp_path

def test_load_skills_recursive_incl_nested(tmp_path):
    root = _mk_instance(tmp_path)
    cards = load_skills(root / "default_app/default_project/agents/everos-m1b-probe/skills")
    assert {c.entry_id for c in cards} == {"sk_test_0001", "everos-test_嵌套 技能：含空格 id"}
    assert all(c.mem_type == "agent_skill" for c in cards)
    assert cards[0].text.startswith("---")  # 全文含 frontmatter

def test_load_entries_by_anchor(tmp_path):
    root = _mk_instance(tmp_path)
    f = root / "default_app/default_project/agents/everos-m1b-probe/.cases/agent_case-2026-01-01.md"
    cards = load_entries(f, prefix="ac", mem_type="agent_case")
    assert [c.entry_id for c in cards] == ["ac_20260101_00000001", "ac_20260101_00000002"]
    assert "合成任务乙" in cards[1].text and "合成任务甲" not in cards[1].text

def test_load_all_cards_counts(tmp_path):
    cards = load_all_cards(_mk_instance(tmp_path))
    assert len(cards) == 4  # 2 skill + 2 case;不含 foresight
