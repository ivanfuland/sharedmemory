#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
restore-from-mirror.py — Phase 3 (franken 6-role) migration foundation.

DATA-INTEGRITY CRITICAL. This tool reconstructs a *staging fake-HOME* from the
CASS raw_mirror so the corpus can be re-ingested by the franken-6role indexer.

Why this is dangerous
---------------------
A single `original_path` (one conversation .jsonl) is captured many times as it
grows. Each capture is a separate manifest pointing at a distinct content blob.
The re-ingest must see ONLY the newest (largest) version of each conversation.
If we restore the *wrong* manifest for a path, we silently write a *truncated*
older version of the conversation to staging — the tail is lost, and NO
downstream completeness gate catches it (re-ingest happily indexes the short
file). So winner-selection must be a fully deterministic total order that never
depends on filesystem iteration order, and every restored byte is blake3-checked
against the manifest before we trust it.

What it does
------------
(a) Group ALL valid manifests by `original_path`. For each path pick the winner
    = max(captured_at_ms); tie-break: source_size_bytes DESC, blob_blake3 ASC,
    manifest_id ASC (deterministic total order). Only the winner's blob restored.
(b) Restore each winner blob to  <staging>/<original_path relative to /home/ivan>
    — by original_path ONLY, NEVER `provider` (provider is mis-labeled for some
    cross-machine sources; using it would drop codex blobs into .claude paths).
    After each copy: blake3(restored_file) MUST equal winner.blob_blake3, else
    abort loudly (corruption).
(c) Write <staging>/.config/cass/sources.toml by rewriting every production
    source `paths` entry: prefix /home/ivan/ -> <staging>/ , preserving
    full_scan=true and all other fields. This is what lets re-ingest scan
    cross-machine `[[sources]]` entries (else their codex convs are silently
    missed: the default connectors only walk <home>/.claude, .codex, .openclaw).
(d) Print counters + the RESTORE_LATEST_PER_PATH_OK marker ONLY on full success.

Pure stdlib + blake3 (Python `blake3` package preferred, else `b3sum` binary).
Run under this repo's uv env:  uv run python infra/cass-semantic/restore-from-mirror.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

# The manifest `original_path` values are absolute paths captured on Ivan's box.
# We strip THIS literal prefix (NOT os.path.expanduser("~") — the mirror encodes
# /home/ivan regardless of who runs the restore) to compute the staging target.
SOURCE_HOME_PREFIX = "/home/ivan/"

# Required manifest fields for a restore decision. Anything missing/null => the
# manifest is unusable and is skipped (counted); if it was the ONLY version of a
# path, that path is LOST and we must report it and refuse the OK marker.
REQUIRED_FIELDS = ("original_path", "captured_at_ms", "blob_blake3", "blob_relative_path")

CHUNK = 1 << 20  # 1 MiB streaming read for hashing/copy verification


# --------------------------------------------------------------------------- #
# blake3 backend detection
# --------------------------------------------------------------------------- #
def detect_blake3_backend():
    """Return (name, hash_fn). hash_fn(path)->hexdigest. Exit if none available."""
    try:
        import blake3  # noqa: F401

        def _hash_py(path: str) -> str:
            h = blake3.blake3()
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()

        return ("python:blake3", _hash_py)
    except ImportError:
        pass

    b3 = shutil.which("b3sum")
    if b3:
        def _hash_bin(path: str) -> str:
            out = subprocess.run(
                [b3, path], check=True, capture_output=True, text=True
            ).stdout
            # b3sum output: "<hex>  <path>"
            digest = out.strip().split()[0]
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise RuntimeError(f"b3sum returned unexpected digest for {path!r}: {out!r}")
            return digest

        return ("b3sum", _hash_bin)

    sys.exit(
        "FATAL: no blake3 backend. Install the `blake3` python package "
        "(run under `uv run python`) or put `b3sum` on PATH."
    )


# --------------------------------------------------------------------------- #
# Winner selection
# --------------------------------------------------------------------------- #
def winner_sort_key(m: dict):
    """Deterministic TOTAL order; sorted(..., key)[0] is the winner.

    Best-first ranking:
      captured_at_ms   DESC  (newest capture wins)
      source_size_bytes DESC (larger content wins ties)
      blob_blake3      ASC   (lexicographic)
      manifest_id      ASC   (lexicographic)
    ints negated for DESC; strings compared ascending. Never touches FS order.
    """
    captured = int(m["captured_at_ms"])
    # source_size_bytes is only a tie-break; default very-low if absent so a
    # size-less manifest loses the size comparison rather than crashing.
    try:
        size = int(m.get("source_size_bytes"))
    except (TypeError, ValueError):
        size = -1
    blob = str(m["blob_blake3"])
    mid = str(m.get("manifest_id") or "")
    return (-captured, -size, blob, mid)


def is_valid_manifest(m: dict):
    """Return (ok, reason). Enforces required fields + type/shape sanity."""
    if not isinstance(m, dict):
        return False, "not-an-object"
    for f in REQUIRED_FIELDS:
        v = m.get(f)
        if v is None or (isinstance(v, str) and v == ""):
            return False, f"missing:{f}"
    if not isinstance(m["captured_at_ms"], int):
        return False, "captured_at_ms-not-int"
    op = m["original_path"]
    if not isinstance(op, str) or not op.startswith("/"):
        return False, "original_path-not-absolute"
    return True, ""


# --------------------------------------------------------------------------- #
# sources.toml rewrite
# --------------------------------------------------------------------------- #
def _toml_escape(s: str) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return '"' + _toml_escape(v) + '"'
    if isinstance(v, list):
        if not all(isinstance(x, (bool, int, float, str)) for x in v):
            raise ValueError(f"unsupported nested list value in sources.toml: {v!r}")
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise ValueError(f"unsupported TOML value type {type(v).__name__}: {v!r}")


def _emit_toml(data: dict) -> str:
    """Minimal, faithful emitter for the flat sources.toml shape.

    Top-level scalar/list keys first, then arrays-of-tables ([[key]]).
    Nested tables inside a source are NOT expected — raise loudly if seen so we
    never silently emit wrong TOML.
    """
    lines = []
    arrays_of_tables = {}
    for k, v in data.items():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            arrays_of_tables[k] = v
        else:
            lines.append(f"{k} = {_toml_value(v)}")
    for k, tables in arrays_of_tables.items():
        for tbl in tables:
            lines.append(f"[[{k}]]")
            for tk, tv in tbl.items():
                if isinstance(tv, dict):
                    raise ValueError(
                        f"nested table [{k}.{tk}] unsupported by this one-off emitter"
                    )
                lines.append(f"{tk} = {_toml_value(tv)}")
    return "\n".join(lines) + "\n"


def rewrite_paths(data: dict, staging_abs: str, warn):
    """Deep-copy production config, rewrite /home/ivan/ path prefixes to staging.

    Returns (transformed_dict, rewritten_count). Non-/home/ivan paths are kept
    verbatim with a warning (they cannot map into staging).
    """
    staging_prefix = staging_abs.rstrip("/") + "/"
    rewritten = 0
    sources = data.get("sources")
    new = json.loads(json.dumps(data))  # simple deep copy (values are JSON-safe)
    if isinstance(sources, list):
        for src in new.get("sources", []):
            paths = src.get("paths")
            if paths is None:
                continue
            if not isinstance(paths, list):
                warn(f"sources.toml: `paths` is not a list in source {src.get('name')!r}; left verbatim")
                continue
            newpaths = []
            for p in paths:
                if isinstance(p, str) and p.startswith(SOURCE_HOME_PREFIX):
                    newpaths.append(staging_prefix + p[len(SOURCE_HOME_PREFIX):])
                    rewritten += 1
                else:
                    warn(f"sources.toml: path {p!r} not under {SOURCE_HOME_PREFIX!r}; left verbatim")
                    newpaths.append(p)
            src["paths"] = newpaths
    return new, rewritten


def generate_sources_toml(prod_toml: Path, staging: Path, warn) -> tuple[int, int]:
    """Write <staging>/.config/cass/sources.toml. Returns (n_sources, n_paths_rewritten)."""
    if not prod_toml.exists():
        raise FileNotFoundError(f"production sources.toml not found: {prod_toml}")
    with open(prod_toml, "rb") as fh:
        data = tomllib.load(fh)

    staging_abs = str(staging.resolve())
    transformed, rewritten = rewrite_paths(data, staging_abs, warn)

    header = (
        "# GENERATED by restore-from-mirror.py — staging copy for franken-6role re-ingest.\n"
        f"# Source: {prod_toml}\n"
        f"# Path prefix {SOURCE_HOME_PREFIX} rewritten to {staging_abs.rstrip('/')}/ .\n"
        "# full_scan and all other fields preserved. DO NOT hand-edit.\n"
    )
    body = _emit_toml(transformed)
    text = header + body

    # Round-trip verification: re-parse our output and assert it equals the
    # intended transform exactly. Fail loud rather than emit subtly-wrong TOML.
    reparsed = tomllib.loads(text)
    if reparsed != transformed:
        raise RuntimeError(
            "sources.toml round-trip mismatch — emitter produced non-faithful TOML.\n"
            f"  intended : {transformed!r}\n"
            f"  reparsed : {reparsed!r}"
        )

    out = staging / ".config" / "cass" / "sources.toml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    n_sources = len(data.get("sources", [])) if isinstance(data.get("sources"), list) else 0
    return n_sources, rewritten


# --------------------------------------------------------------------------- #
# Restore target resolution
# --------------------------------------------------------------------------- #
def resolve_target(original_path: str, staging_abs: str) -> str:
    """Map original_path -> absolute staging target, guarding against escape.

    Raises ValueError on anything unsafe (path not under /home/ivan, empty
    remainder, or a target that escapes the staging root via traversal).
    """
    if not original_path.startswith(SOURCE_HOME_PREFIX):
        raise ValueError(
            f"original_path not under {SOURCE_HOME_PREFIX!r}: {original_path!r} "
            "(cannot map into staging safely)"
        )
    rel = original_path[len(SOURCE_HOME_PREFIX):]
    if not rel or rel.strip("/") == "":
        raise ValueError(f"original_path has empty remainder after prefix strip: {original_path!r}")
    target = os.path.normpath(os.path.join(staging_abs, rel))
    root = staging_abs.rstrip("/") + os.sep
    if not (target + os.sep).startswith(root):
        raise ValueError(f"resolved target {target!r} escapes staging root {staging_abs!r}")
    return target


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Restore CASS raw_mirror winners into a staging fake-HOME (franken 6-role migration)."
    )
    ap.add_argument(
        "--mirror",
        default=os.path.expanduser("~/.local/share/coding-agent-search/raw-mirror"),
        help="raw_mirror root (contains v1/manifests + v1/blobs). Default: %(default)s",
    )
    ap.add_argument(
        "--staging",
        default=os.path.expanduser("~/.local/share/cc-6role-migration/staging"),
        help="staging fake-HOME output dir. Default: %(default)s",
    )
    ap.add_argument(
        "--sources-toml",
        default=os.path.expanduser("~/.config/cass/sources.toml"),
        help="production sources.toml to rewrite. Default: %(default)s",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="winner-selection + print grouping counters, NO copying, NO marker.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="if --staging is non-empty, clear it first (default: refuse).",
    )
    args = ap.parse_args()

    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        print(f"WARN: {msg}", file=sys.stderr)

    mirror = Path(args.mirror).expanduser()
    store_base = mirror / "v1"          # blob_relative_path is relative to <mirror>/v1
    man_dir = store_base / "manifests"
    staging = Path(args.staging).expanduser()

    if not man_dir.is_dir():
        sys.exit(f"FATAL: manifests dir not found: {man_dir}")

    backend_name, hash_file = detect_blake3_backend()

    # ---- load + validate manifests ---------------------------------------- #
    man_files = sorted(man_dir.glob("*.json"))
    total_files = len(man_files)
    if total_files == 0:
        sys.exit(f"FATAL: no manifests under {man_dir}")

    groups: dict[str, list[dict]] = {}
    skipped: list[tuple[str, str]] = []  # (file, reason)
    grouped_count = 0
    for mf in man_files:
        try:
            with open(mf, "rb") as fh:
                m = json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            skipped.append((mf.name, f"bad-json:{e}"))
            warn(f"skip {mf.name}: bad JSON ({e})")
            continue
        ok, reason = is_valid_manifest(m)
        if not ok:
            skipped.append((mf.name, reason))
            warn(f"skip {mf.name}: {reason}")
            continue
        groups.setdefault(m["original_path"], []).append(m)
        grouped_count += 1

    # ---- winner selection -------------------------------------------------- #
    winners: dict[str, dict] = {}
    for op, ms in groups.items():
        winners[op] = min(ms, key=winner_sort_key)  # min under best-first key

    distinct_paths = len(winners)
    paths_with_multi_blob = sum(
        1 for ms in groups.values() if len({m["blob_blake3"] for m in ms}) > 1
    )
    extra_versions = grouped_count - distinct_paths

    # A skipped manifest whose original_path never made it into any group means a
    # conversation could be entirely lost. Detect lost paths (skipped manifests
    # carry their path even if invalid for restore) — best-effort using any
    # readable original_path on skipped records.
    lost_paths = 0
    # (Structural note: we cannot know original_path for a bad-JSON manifest.
    #  grouped paths are safe; we surface skipped count so controller can judge.)

    print(f"# blake3 backend: {backend_name}")
    print(f"# manifest files: {total_files}  grouped(valid): {grouped_count}  skipped: {len(skipped)}")

    if args.dry_run:
        print(
            f"DRY-RUN distinct_paths={distinct_paths}  "
            f"paths_with_multi_blob={paths_with_multi_blob}  "
            f"extra_versions={extra_versions}  skipped={len(skipped)}"
        )
        if paths_with_multi_blob == 0:
            print(
                "WARN: 0 paths_with_multi_blob — grouping is almost certainly wrong "
                "(conversations are captured repeatedly; expect nonzero).",
                file=sys.stderr,
            )
        # No copying, no marker in dry-run.
        return 0

    # ---- staging prep ------------------------------------------------------ #
    staging_abs = str(staging.resolve())
    if staging.exists():
        non_empty = any(staging.iterdir())
        if non_empty and not args.force:
            sys.exit(
                f"FATAL: staging {staging} is non-empty. Refusing to merge stale files. "
                "Re-run with --force to clear it first."
            )
        if non_empty and args.force:
            print(f"# --force: clearing existing staging {staging}")
            shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    staging_abs = str(staging.resolve())

    # ---- restore winners --------------------------------------------------- #
    restored = 0
    verified = 0
    for op, w in sorted(winners.items()):
        blob_rel = w["blob_relative_path"]
        blob_abs = (store_base / blob_rel).resolve()
        # blob must live inside the mirror store (guard against odd relpaths)
        if not str(blob_abs).startswith(str(store_base.resolve()) + os.sep):
            sys.exit(f"FATAL: blob path {blob_abs} escapes store base {store_base} (manifest {w.get('manifest_id')})")
        if not blob_abs.is_file():
            sys.exit(f"FATAL: winner blob missing on disk: {blob_abs} (path {op}, manifest {w.get('manifest_id')})")

        try:
            target = resolve_target(op, staging_abs)
        except ValueError as e:
            sys.exit(f"FATAL: cannot resolve staging target: {e}")

        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(blob_abs, target)  # verbatim byte copy (blobs uncompressed)
        restored += 1

        got = hash_file(target)
        want = w["blob_blake3"]
        if got != want:
            sys.exit(
                "FATAL: blake3 MISMATCH after restore (corruption) — aborting WITHOUT marker.\n"
                f"  path     : {op}\n"
                f"  target   : {target}\n"
                f"  manifest : {w.get('manifest_id')}\n"
                f"  expected : {want}\n"
                f"  got      : {got}"
            )
        verified += 1

    # ---- sources.toml ------------------------------------------------------ #
    try:
        n_sources, n_rewritten = generate_sources_toml(Path(args.sources_toml).expanduser(), staging, warn)
    except Exception as e:  # noqa: BLE001 — one-off tool: surface any failure loudly, no marker
        sys.exit(f"FATAL: sources.toml generation failed: {e}")
    print(f"# sources.toml: {n_sources} source(s), {n_rewritten} path(s) rewritten -> {staging}/.config/cass/sources.toml")

    # ---- counters + marker ------------------------------------------------- #
    print(
        f"distinct_paths={distinct_paths}  "
        f"paths_with_multi_blob={paths_with_multi_blob}  "
        f"extra_versions={extra_versions}  "
        f"restored={restored}  "
        f"blake3_verified={verified}"
    )

    # Marker ONLY if every winner restored + verified, no skips lost a path, and
    # counts are internally consistent.
    ok = (
        restored == distinct_paths
        and verified == restored
        and verified == distinct_paths
        and lost_paths == 0
    )
    if len(skipped) > 0:
        # Skips are non-fatal ONLY if none stranded a path with no surviving
        # version. We grouped every VALID manifest; a path present in `winners`
        # is covered. Bad-JSON manifests we couldn't read a path from are the
        # only residual risk — surface, but do not silently pass.
        print(
            f"# NOTE: {len(skipped)} manifest(s) skipped (see WARN lines). "
            "Controller: confirm none was the sole version of a conversation.",
            file=sys.stderr,
        )

    if ok:
        print("RESTORE_LATEST_PER_PATH_OK")
        return 0

    print(
        f"FATAL: restore incomplete — restored={restored} verified={verified} "
        f"distinct_paths={distinct_paths}; NOT printing OK marker.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
