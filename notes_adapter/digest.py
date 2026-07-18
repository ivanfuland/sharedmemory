"""Canonical digest + three-store (SQLite / Lance / topic markdown) verify.

Frozen formula (spec, do not alter): sha256 over sorted "key=value" lines,
no trailing newline. `verify_rows` is a hard consistency gate — it must
genuinely catch single-leg drift (a mismatch confined to one of the three
stores), not just agree when all three already match.
"""
import hashlib
from dataclasses import dataclass


def canonical_digest(summary, topic_name, topic_path, category_id, depth, content) -> str:
    fields = {
        "frontmatter:summary": summary,
        "frontmatter:topic_name": topic_name,
        "frontmatter:topic_path": topic_path,
        "frontmatter:category_id": category_id,
        "frontmatter:depth": str(depth),
        "body": content.rstrip(),
    }
    blob = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class TopicRow:
    node_id: str
    summary: str
    topic_name: str
    topic_path: str
    category_id: str
    depth: int
    content: str
    content_sha256: str


def verify_rows(sqlite_rows, lance_digests, topic_markdowns) -> list[str]:
    import yaml as _y

    bad = []
    sql_ids = {r.node_id for r in sqlite_rows}
    if not (sql_ids == set(lance_digests) == set(topic_markdowns)):
        # R7 P1-4: 三组 node_id 先集合等价（含 markdown 孤儿）
        return ["node_id sets differ"]
    for r in sqlite_rows:
        d_sql = canonical_digest(r.summary, r.topic_name, r.topic_path, r.category_id, r.depth, r.content)
        if lance_digests.get(r.node_id) != d_sql:
            bad.append(f"lance!=sqlite {r.node_id}")
        md = topic_markdowns.get(r.node_id)
        if md is None:
            bad.append(f"markdown missing {r.node_id}")
            continue
        head, _, body = md.partition("\n---\n")
        fm = _y.safe_load(head.lstrip("-\n")) or {}
        d_md = canonical_digest(
            fm.get("summary", ""),
            fm.get("topic_name", ""),
            fm.get("topic_path", ""),
            fm.get("category_id", ""),
            int(fm.get("depth", 0)),
            body,
        )
        if d_md != d_sql:
            bad.append(f"markdown!=sqlite {r.node_id}")
    return bad
