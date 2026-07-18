"""Frontmatter 规范化 + 严格 YAML loader + ingest fingerprint (spec §5.2).

prepare_document 是唯一付费入口：verify staged_sha256 → parse_strict →
normalize → redact_secrets → content_hash → ingest_fingerprint → 组装
PreparedDocument。HttpRemote.post 只收 PreparedDocument。
"""
import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

from cass_corpus.redact import redact_secrets

from . import identity


class DuplicateKeyError(ValueError): ...


class MalformedFrontmatterError(ValueError): ...


class _StrictLoader(yaml.SafeLoader): ...


def _mapping(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise DuplicateKeyError(str(dupes))
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_strict(text: str) -> dict:
    out = yaml.load(text, Loader=_StrictLoader)
    return out if isinstance(out, dict) else {}


@dataclass
class NormalizedFM:
    title: str
    date: str | None
    category: str
    tags: list[str]
    warnings: list[str] = field(default_factory=list)


def normalize(fm: dict, filename_stem: str, taxonomy: set[str]) -> NormalizedFM:
    w: list[str] = []
    title = fm.get("title")
    if not isinstance(title, str) or not title.strip():
        title = filename_stem

    raw_date, date = fm.get("date"), None
    if raw_date is not None:
        s = raw_date.isoformat() if hasattr(raw_date, "isoformat") else str(raw_date)
        try:
            (_dt.datetime.fromisoformat(s) if len(s) > 10 else _dt.date.fromisoformat(s))
            date = s
        except ValueError:
            date = None
        if date is None:
            w.append(f"bad date: {raw_date!r}")
        else:
            # 时区判定用 fromisoformat 结果的 tzinfo，None 才补 +08:00（R4 P2-1）
            parsed = _dt.datetime.fromisoformat(s) if len(s) > 10 else None
            date = (s + "+08:00") if (parsed is not None and parsed.tzinfo is None) else s

    cats = fm.get("categories")
    cand = [c for c in (cats if isinstance(cats, list) else [cats]) if isinstance(c, str) and c.strip()]
    for c in (cats if isinstance(cats, list) else [cats]):
        if c is not None and (not isinstance(c, str) or (c not in taxonomy)):
            w.append(f"category dropped: {c!r}")
    category = next((c for c in cand if c in taxonomy), None)
    if category is None:
        category = "Others"
        w.append(f"category fallback: {cats!r}")  # 缺失/空/全非法统一记 warning（R6 P2-13）

    raw_tags = fm.get("tags")
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    tags: list[str] = []
    seen: set[str] = set()
    if raw_tags is None:
        pass
    elif isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str) and t not in seen:
                tags.append(t)
                seen.add(t)
            elif not isinstance(t, str):
                w.append(f"tag dropped: {t!r}")
    else:
        w.append(f"tags dropped: {raw_tags!r}")

    return NormalizedFM(title, date, category, tags, w)


def ingest_fingerprint(redacted_hash: str, cfg: dict) -> str:
    payload = {"schema_version": 1, "redacted_hash": redacted_hash, **cfg}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(blob, digest_size=16).hexdigest()


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split a staged markdown file into (yaml_frontmatter_text, body).

    No frontmatter opener → ("", text): a legitimately frontmatter-less
    document (prepare_document then parse_strict("") -> {} -> category
    "Others", which is correct).

    Frontmatter opener present but the closing delimiter is never found →
    raises MalformedFrontmatterError. A file that opens frontmatter but
    never closes it is a truncation/corruption signal, not a legitimate
    no-frontmatter file — it must fail loud, because prepare_document is
    the sole paid ingest entry point.

    Search starts at index 3 (not 4) so an empty frontmatter block
    "---\n---\nbody\n" is detected: the closing "\n---\n" for an empty
    block begins at index 3 (the trailing \n of the opener is shared with
    the leading \n of the closer), which a search starting at 4 would miss.
    """
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 3)
    if end == -1:
        raise MalformedFrontmatterError("unterminated frontmatter delimiter")
    return text[4 : end + 1], text[end + 5 :]


@dataclass
class PreparedDocument:
    source_token: str
    title: str
    category_id: str
    redacted: bytes
    raw_hash: str  # 原文件字节 hash = ledger desired_hash（diff 判据），passthrough 自 ref.vault_raw_hash
    redacted_hash: str  # 脱敏后字节 hash，进 fingerprint
    fingerprint: str


def prepare_document(ref, staged_bytes: bytes, generation: int, taxonomy, cfg) -> PreparedDocument:
    """唯一付费入口：verify staged_sha256 → parse_strict → normalize →
    redact_secrets → content_hash → ingest_fingerprint → 组装。"""
    if hashlib.sha256(staged_bytes).hexdigest() != ref.staged_sha256:
        raise ValueError(
            f"staged_sha256 mismatch for {ref.canonical_path!r}: staged bytes do not match SampleRef"
        )

    # Invalid UTF-8 intentionally raises UnicodeDecodeError (a ValueError
    # subclass) — fail-loud is intended; staged files are guaranteed UTF-8 markdown.
    text = staged_bytes.decode("utf-8")
    fm_text, _body = _split_frontmatter(text)
    fm = parse_strict(fm_text)

    stem = PurePosixPath(ref.canonical_path).stem
    n = normalize(fm, stem, taxonomy)

    redacted_bytes = redact_secrets(text).encode("utf-8")
    redacted_hash = identity.content_hash(redacted_bytes)
    fp = ingest_fingerprint(redacted_hash, cfg)
    token = identity.source_token(ref.canonical_path, generation)

    return PreparedDocument(
        source_token=token,
        title=n.title,
        category_id=n.category,
        redacted=redacted_bytes,
        raw_hash=ref.vault_raw_hash,
        redacted_hash=redacted_hash,
        fingerprint=fp,
    )
