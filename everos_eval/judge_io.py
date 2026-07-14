"""judge 任务包(自包含 jsonl)与回执解析(按候选粒度校验,坏行重试不静默丢)。"""
from __future__ import annotations
import json
from pathlib import Path

_FS_CATS = {"insight", "instruction_echo", "trivial"}


def build_l1_jobs(queries, cards):
    return [{"job_id": f"l1:{q['query_id']}:{c.entry_id}", "kind": "l1",
             "query": q["query"], "card_id": c.entry_id, "card_type": c.mem_type,
             "card_text": c.text} for q in queries for c in cards]


def build_top5_jobs(query, top5, card_text_by_id):
    return [{"job_id": f"top5:{query['query_id']}:{rank}:{it['id']}", "kind": "top5",
             "query": query["query"], "rank": rank, "card_id": it["id"],
             "card_type": it["mem_type"], "card_text": card_text_by_id[it["id"]]}
            for rank, it in enumerate(top5, 1)]


def build_foresight_jobs(entries):
    return [{"job_id": f"fs:{c.entry_id}", "kind": "foresight", "entry_text": c.text}
            for c in entries]


def _valid(v, kind) -> bool:
    if not isinstance(v.get("job_id"), str) or not isinstance(v.get("reason"), str):
        return False
    if kind == "foresight":
        return v.get("category") in _FS_CATS
    if not isinstance(v.get("relevant"), bool) or not isinstance(v.get("useful"), bool):
        return False
    return not (v["useful"] and not v["relevant"])  # useful ⇒ relevant


_KIND_PREFIX = {"l1": "l1", "top5": "top5", "foresight": "fs"}


def parse_verdicts(path: Path, expected_kind: str, expected_job_ids: set | None = None):
    prefix = _KIND_PREFIX[expected_kind] + ":"
    ok, failed, seen = {}, [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            failed.append(f"unparseable:{line[:80]}")
            continue
        jid = v.get("job_id")
        if not isinstance(jid, str) or not jid.startswith(prefix):
            failed.append(f"wrong_kind:{jid}")
            continue
        if jid in seen:
            failed.append(f"duplicate:{jid}")
            continue
        seen.add(jid)
        if expected_job_ids is not None and jid not in expected_job_ids:
            failed.append(f"unexpected:{jid}")
            continue
        (ok.__setitem__(jid, v) if _valid(v, expected_kind) else failed.append(jid))
    if expected_job_ids is not None:
        failed.extend(f"missing:{j}" for j in sorted(expected_job_ids - seen))
    return ok, failed
