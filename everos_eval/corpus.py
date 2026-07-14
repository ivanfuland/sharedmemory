"""解析 EverOS data_dir 的 canonical 卡语料(只读)。M1c Phase 1 评估仪器。"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path

_FM_ID = re.compile(r"^id:\s*(.+?)\s*$", re.M)  # 真实 skill id 含空格/中文/冒号(codex R2 实读)
_FM_NAME = re.compile(r"^name:\s*(.+?)\s*$", re.M)


@dataclass(frozen=True)
class Card:
    entry_id: str
    mem_type: str
    title: str
    text: str


def load_skills(skills_dir: Path) -> list[Card]:
    """递归找全部 SKILL.md(含标题带 / 形成的嵌套目录)。entry_id 取 frontmatter id。"""
    cards: list[Card] = []
    for p in sorted(skills_dir.rglob("SKILL.md")):
        raw = p.read_text(encoding="utf-8")
        m = _FM_ID.search(raw)
        if not m:
            raise ValueError(f"SKILL.md missing frontmatter id: {p}")
        name = _FM_NAME.search(raw)
        cards.append(Card(m.group(1), "agent_skill", name.group(1) if name else p.parent.name, raw))
    return cards


def load_entries(md_file: Path, prefix: str, mem_type: str) -> list[Card]:
    """按 <!-- entry:<prefix>_... --> 锚切聚合 markdown 为逐条 Card。"""
    raw = md_file.read_text(encoding="utf-8")
    anchor = re.compile(rf"<!-- entry:({prefix}_\d{{8}}_\d{{8}}) -->")
    parts = anchor.split(raw)
    cards: list[Card] = []
    for i in range(1, len(parts) - 1, 2):
        eid, body = parts[i], parts[i + 1]
        cards.append(Card(eid, mem_type, eid, body.strip()))
    if len(parts) >= 2 and len(parts) % 2 == 0:  # 末条无后继锚
        cards.append(Card(parts[-1], mem_type, parts[-1], ""))
    return cards


def load_all_cards(instance_root: Path) -> list[Card]:
    """skill + case 全量(gold 语料,foresight 另走 load_entries)。"""
    agent_dir = instance_root / "default_app/default_project/agents/everos-m1b-probe"
    cards = load_skills(agent_dir / "skills")
    for f in sorted((agent_dir / ".cases").glob("agent_case-*.md")):
        cards.extend(load_entries(f, prefix="ac", mem_type="agent_case"))
    return cards
