"""M0 terminal-state: scan EverOS markdown output for a session's agent_case.

markdown 是 EverOS 的 source-of-truth 公开契约（不是内部 DB schema），所以拿它当
终态判据对黑盒友好且便宜。`/search` 做不了这件事：它强制非空 query，agent 搜索固定
把 case+skill 一起返回，而 `agent_skill` 压根没有 `session_id` —— 那不是
「列出本 session 产物」的 API。

格式取自 M0 真实产出：
    <!-- entry:ac_20260709_00000001 -->
    ## ac_20260709_00000001

    **owner_id**: ivan-coding
    **session_id**: m0-synth-0001
    ...
    <!-- /entry:ac_20260709_00000001 -->
"""

from __future__ import annotations

import pathlib
import re

CASE_GLOB = "agent_case-*.md"

_SID = re.compile(r"^\*\*session_id\*\*:\s*(?P<sid>\S+)\s*$", re.MULTILINE)
# 开标记（`<!-- entry:` 不会误吃闭标记 `<!-- /entry:`）
_ENTRY_OPEN = re.compile(r"<!--\s*entry:(?P<eid>[^\s/>]+)\s*-->")
# 一个完整 entry block：开标记 → 同名闭标记
_ENTRY_BLOCK = re.compile(
    r"<!--\s*entry:(?P<eid>[^\s/>]+)\s*-->(?P<body>.*?)<!--\s*/entry:(?P=eid)\s*-->",
    re.DOTALL,
)


def session_ids_in(md_text: str) -> list[str]:
    return [m.group("sid") for m in _SID.finditer(md_text)]


def session_has_case(md_text: str, session_id: str) -> bool:
    return session_id in session_ids_in(md_text)


def entry_ids_in(md_text: str) -> list[str]:
    return [m.group("eid") for m in _ENTRY_OPEN.finditer(md_text)]


def session_case_entry_ids(md_text: str, session_id: str) -> list[str]:
    """本 session 产的 case_entry_id 列表（供 M1 的 A→B 溯源桥）。

    必须按 entry block 归属，不能全文匹配 —— 一个 daily md 里多个 session 的
    条目混在一起，串了就把攻略指向错误的原始会话。
    """
    out: list[str] = []
    for m in _ENTRY_BLOCK.finditer(md_text):
        if session_id in session_ids_in(m.group("body")):
            out.append(m.group("eid"))
    return out


def find_session_case_files(memory_root: str, session_id: str) -> list[pathlib.Path]:
    root = pathlib.Path(memory_root).expanduser()
    if not root.exists():
        return []
    return [f for f in sorted(root.rglob(CASE_GLOB)) if session_has_case(f.read_text(), session_id)]


def session_extracted(memory_root: str, session_id: str) -> bool:
    return len(find_session_case_files(memory_root, session_id)) > 0


def collect_case_entry_ids(memory_root: str, session_id: str) -> list[str]:
    out: list[str] = []
    for f in find_session_case_files(memory_root, session_id):
        out.extend(session_case_entry_ids(f.read_text(), session_id))
    return out
