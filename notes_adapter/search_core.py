"""Retrieval core library — spec §6 ("检索折叠/富化核心库在 M0 落地") + §8 consumption
state matrix. `notes_search` (M2) is a thin MCP wrapper around `search()`; all
folding/filtering/staleness logic lives here so it is unit-testable without a
live EverOS instance or an MCP host.

Two layers:
  - `eligibility()` — the single source of truth for spec §8's consumption
    state matrix, shared verbatim with `lookup_core.py` (R3 P1-10: search and
    lookup must not fork their own copies of this decision table).
  - `fold_and_filter()` — pure function, frozen contract (task brief Step 1):
    fold raw search hits by doc_id, keep only admitted+eligible docs.
  - `search()` — the HTTP shell: call `/api/v1/knowledge/search`, unwrap the
    envelope, fold, join the ledger, apply best-effort tag/date filters,
    return `Hit` objects.

No network in this module's test suite — `search()` takes an injectable
`http_post` callable so tests use an in-memory fake. Live contract
validation against a real instance is a later task (mirrors test_http_remote.py's
split for `HttpRemote`).
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# eligibility() — spec §8 consumption state matrix, single source of truth.
# ---------------------------------------------------------------------------

# Statuses where an update is mid-flight (staging in progress, old active
# still serving) — spec §5.1 status enum minus the terminal/retiring ones.
_INFLIGHT_STATUSES = {"pending", "in_flight", "indexing", "failed"}
_RETIRED_STATUSES = {"pending_repair", "retiring", "deleted", "dead"}


@dataclass(frozen=True)
class Eligibility:
    """Per-row visibility decision, spec §8. Four independent axes because
    `notes_search` and `notes_lookup` (metadata, with/without `include_status`,
    and body) each consume a different slice of the same matrix row."""

    search_visible: bool
    lookup_default_visible: bool  # notes_lookup metadata, no include_status flag
    lookup_include_status_visible: bool  # notes_lookup metadata, include_status=True
    body_allowed: bool
    stale: bool


_NOT_ELIGIBLE = Eligibility(False, False, False, False, False)
_NOT_ELIGIBLE_BUT_DEBUGGABLE = Eligibility(False, False, True, False, False)


def eligibility(operation: str, active_doc_id, admitted: bool, status: str) -> Eligibility:
    """spec §8 matrix, verbatim:

    | 台账状态                              | active? | search           | lookup 元数据            | lookup 正文 |
    |---------------------------------------|---------|------------------|--------------------------|-------------|
    | synced                                | 是      | 正常返回          | 返回                      | 可           |
    | in_flight/indexing/failed/pending(update)| 是(旧代)| 返回+stale:true  | 返回+status               | 可,标 stale  |
    | in_flight/…(首建,无 active)            | 否      | 不可见            | 默认排除;include_status可见 | 否          |
    | operation=delete(pending/in_flight)   | 是(旧代残留)| 不可见         | 默认排除;include_status可见 | 否          |
    | pending_repair / retiring             | 视残留  | 不可见            | 默认排除;include_status可见 | 否          |
    | deleted / dead                        | 否      | 不可见            | 默认排除;include_status可见 | 否          |

    `admitted=False` (path撤出白名单, spec §5.3) is an unconditional override —
    "检索面立即隐藏" applies to every axis, `include_status` included: admitted
    gates whitelist membership, not sync/delete bookkeeping, so there is no
    debugging escape hatch for it the way there is for the status matrix.
    """
    if not admitted:
        return _NOT_ELIGIBLE

    has_active = active_doc_id is not None

    if status == "synced":
        return Eligibility(True, True, True, True, False)

    if status in _INFLIGHT_STATUSES:
        if operation == "update" and has_active:
            return Eligibility(True, True, True, True, True)
        if operation == "delete" and has_active:
            # 删除意图确认即隐藏（§8 nit8）：即便旧代仍残留可读，也不再展示。
            return _NOT_ELIGIBLE_BUT_DEBUGGABLE
        # 首建（operation=create），尚无 active：not yet visible to anyone but
        # an operator asking with include_status.
        return _NOT_ELIGIBLE_BUT_DEBUGGABLE

    if status in _RETIRED_STATUSES:
        return _NOT_ELIGIBLE_BUT_DEBUGGABLE

    # Unknown/unmodeled status: fail closed, not visible anywhere.
    return _NOT_ELIGIBLE


# ---------------------------------------------------------------------------
# fold_and_filter() — frozen pure function (task brief Step 1).
# ---------------------------------------------------------------------------


def fold_and_filter(raw: list[dict], ledger: dict, top_k: int) -> tuple[list[dict], bool]:
    """Fold `raw` (a list of {"doc_id":.., "score":.., ...} dicts, already in
    upstream rank order) by doc_id — keep first-seen order, best_score = max
    score seen for that doc_id, cards = every raw entry folded into it — then
    keep only docs present in `ledger` with `admitted=True` (eligibility, when
    the ledger rows carry the extra fields, is applied by the caller; this
    function's contract is the frozen Step-1 test: fold + admitted filter
    only). Truncate to `top_k`. `partial=True` whenever the truncated result
    has fewer than `top_k` hits — either the raw candidate pool ran out, or
    enough of it got filtered out that we can't promise the true top-k.
    """
    order: list[str] = []
    best_score: dict[str, float | None] = {}
    cards: dict[str, list[dict]] = {}
    for h in raw:
        doc_id = h["doc_id"]
        score = h.get("score")
        if doc_id not in best_score:
            order.append(doc_id)
            best_score[doc_id] = score
            cards[doc_id] = []
        elif score is not None and (best_score[doc_id] is None or score > best_score[doc_id]):
            best_score[doc_id] = score
        cards[doc_id].append(h)

    hits: list[dict] = []
    for doc_id in order:
        entry = ledger.get(doc_id)
        if entry is None or not entry.get("admitted", False):
            continue
        hits.append(
            {
                "doc_id": doc_id,
                "path": entry["path"],
                "best_score": best_score[doc_id],
                "cards": cards[doc_id],
            }
        )

    truncated = hits[:top_k]
    partial = len(truncated) < top_k
    return truncated, partial


# ---------------------------------------------------------------------------
# search() — the HTTP shell.
# ---------------------------------------------------------------------------


@dataclass
class Hit:
    path: str
    doc_id: str
    best_score: float | None
    cards: list = field(default_factory=list)
    synced_frontmatter: dict | None = None
    partial: bool = False
    stale: bool = False
    status: str | None = None
    metadata_stale: bool = False


def _unwrap_envelope(payload: dict) -> dict:
    if not isinstance(payload, dict) or "data" not in payload:
        raise ValueError(f"search: response missing envelope 'data' key: {payload!r}")
    return payload["data"]


def _default_http_post(base_url: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _frontmatter_matches_tags(fm: dict | None, tags: list[str]) -> bool:
    if not fm:
        return False
    have = set(fm.get("tags") or [])
    return bool(have & set(tags))


def _frontmatter_in_date_range(fm: dict | None, date_range: tuple[str, str]) -> bool:
    if not fm:
        return False
    d = fm.get("date")
    if not d:
        return False
    lo, hi = date_range
    return lo <= d <= hi


def search(
    base_url: str,
    ledger: dict,
    query: str,
    top_k: int = 5,
    overfetch_mult: int = 4,
    tags: list[str] | None = None,
    date_range: tuple[str, str] | None = None,
    *,
    http_post=None,
) -> list[Hit]:
    """Call `/api/v1/knowledge/search`, fold+filter, join the ledger, apply
    best-effort tag/date filters (spec §8: "tag/date 过滤标 best-effort" —
    they operate on `synced_frontmatter`, the already-promoted active-代
    metadata, not the latest scan, and they do not widen the overfetch to
    compensate for what they drop).

    `ledger` is keyed by **doc_id** (the shape `fold_and_filter`'s frozen test
    uses), one entry per row that currently has an `active_doc_id`. Building
    that doc_id-keyed view out of the path-keyed `notes_ledger` table is a
    caller concern (the M2 MCP shell); this module only consumes it. Each
    entry needs: `path`, `admitted`, `active_doc_id`, `operation`, `status`,
    `synced_frontmatter`, and optionally `frontmatter` (latest scan, for
    `metadata_stale` — spec nit7).
    """
    poster = http_post or _default_http_post
    request_top_k = min(top_k * overfetch_mult, 100)
    payload = poster(base_url, "/api/v1/knowledge/search", {"query": query, "top_k": request_top_k})
    data = _unwrap_envelope(payload)
    raw_hits = data.get("hits")
    if not isinstance(raw_hits, list):
        raise ValueError(f"search: response missing hits array: {payload!r}")

    flat: list[dict] = []
    for h in raw_hits:
        doc = h.get("document") if isinstance(h, dict) else None
        if not isinstance(doc, dict) or not doc.get("doc_id"):
            raise ValueError(f"search: hit missing document.doc_id: {h!r}")
        flat.append({"doc_id": doc["doc_id"], "score": h.get("score"), "document": doc})

    folded, partial = fold_and_filter(flat, ledger, top_k)

    hits: list[Hit] = []
    for f in folded:
        entry = ledger[f["doc_id"]]
        elig = eligibility(
            entry.get("operation", "none"),
            entry.get("active_doc_id"),
            entry.get("admitted", False),
            entry.get("status", "synced"),
        )
        if not elig.search_visible:
            continue

        synced_fm = entry.get("synced_frontmatter")
        latest_fm = entry.get("frontmatter")
        metadata_stale = latest_fm is not None and latest_fm != synced_fm

        if tags and not _frontmatter_matches_tags(synced_fm, tags):
            continue
        if date_range and not _frontmatter_in_date_range(synced_fm, date_range):
            continue

        hits.append(
            Hit(
                path=f["path"],
                doc_id=f["doc_id"],
                best_score=f["best_score"],
                cards=f["cards"],
                synced_frontmatter=synced_fm,
                partial=partial,
                stale=elig.stale,
                status=entry.get("status"),
                metadata_stale=metadata_stale,
            )
        )

    return hits
