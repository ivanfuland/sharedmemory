"""Retrieval core library — `notes_lookup` half (spec §6/§8, task brief Step
4.5). `eligibility()` is imported (not reimplemented) from `search_core` —
R3 P1-10: search and lookup must share one decision table, never fork their
own copies of spec §8's consumption state matrix.

`lookup()` takes a **path-keyed** ledger (the real `notes_ledger` table's
natural shape — PK is `path`), unlike `search_core.search()`'s doc_id-keyed
ledger param: lookup is driven by known paths (or a date filter over all of
them), search is driven by doc_id hits coming back from the search API.

`body()` is the safe-open + same-version-redact + size-cap read path spec §8
describes for `notes_lookup` 正文 ("同版 redact + content_source/current_hash/
synced_hash + stale + 尺寸上限 + truncation 契约").
"""
from __future__ import annotations

from dataclasses import dataclass

from cass_corpus.redact import redact_secrets

from . import identity, safe_open
from .search_core import _frontmatter_in_date_range, eligibility

DEFAULT_BODY_MAX_CHARS = 4000


@dataclass
class BodyResult:
    path: str
    content: str
    truncated: bool
    current_hash: str
    synced_hash: str | None
    content_stale: bool


def body(vault_root: str, path: str, row: dict, *, max_chars: int = DEFAULT_BODY_MAX_CHARS) -> BodyResult:
    """safe-open re-read of the live vault file -> same-version redact
    (`cass_corpus.redact_secrets`, the exact function `frontmatter.prepare_document`
    uses at ingest — "同版") -> size cap -> current/synced hash + content_stale.

    `current_hash` is computed over the **raw** re-read bytes (not the
    redacted text) so it is directly comparable to `row["synced_hash"]`,
    which spec §5.1 defines as `blake2b(文件字节, ...)` over raw bytes — the
    two hashes must use the same input space or "content_stale" would be
    comparing apples to oranges.
    """
    raw = safe_open.read_within(vault_root, path)
    current_hash = identity.content_hash(raw)
    redacted_text = redact_secrets(raw.decode("utf-8"))
    truncated = len(redacted_text) > max_chars
    content = redacted_text[:max_chars]
    synced_hash = row.get("synced_hash")
    content_stale = synced_hash is not None and current_hash != synced_hash
    return BodyResult(
        path=path,
        content=content,
        truncated=truncated,
        current_hash=current_hash,
        synced_hash=synced_hash,
        content_stale=content_stale,
    )


def lookup(
    vault_root: str,
    ledger: dict,
    paths: list[str] | None = None,
    date_range: tuple[str, str] | None = None,
    include_status: bool = False,
    want_body: bool = False,
    max_chars: int = DEFAULT_BODY_MAX_CHARS,
) -> list[dict]:
    """`ledger` is keyed by **path** (real notes_ledger PK shape). `paths`
    restricts to a known subset; `None` means "every row currently in the
    ledger" (best-effort date filter still applies on top).

    Body content is fetched (safe-open + redact) only when the row's
    eligibility permits it (`body_allowed`) *and* the caller asked for it
    (`want_body=True`) — a metadata-only lookup never touches the vault
    filesystem.
    """
    rows: list[dict] = []
    candidate_paths = paths if paths is not None else list(ledger.keys())

    for path in candidate_paths:
        row = ledger.get(path)
        if row is None:
            continue

        elig = eligibility(
            row.get("operation", "none"),
            row.get("active_doc_id"),
            row.get("admitted", False),
            row.get("status", "synced"),
        )
        visible = elig.lookup_include_status_visible if include_status else elig.lookup_default_visible
        if not visible:
            continue

        synced_fm = row.get("synced_frontmatter")
        if date_range and not _frontmatter_in_date_range(synced_fm, date_range):
            continue

        out = {
            "path": path,
            "doc_id": row.get("active_doc_id"),
            "status": row.get("status"),
            "synced_frontmatter": synced_fm,
            "stale": elig.stale,
        }

        if want_body and elig.body_allowed:
            b = body(vault_root, path, row, max_chars=max_chars)
            out["content"] = b.content
            out["content_truncated"] = b.truncated
            out["current_hash"] = b.current_hash
            out["synced_hash"] = b.synced_hash
            out["content_stale"] = b.content_stale or elig.stale
        else:
            out["content"] = None

        rows.append(out)

    return rows
