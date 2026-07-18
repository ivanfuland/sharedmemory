"""Identity primitives frozen by spec §5.1 — do not change without spec bump."""
import hashlib, unicodedata

class TokenCollisionError(RuntimeError): ...

def canonical_path(p: str) -> str:
    if "\\" in p:
        raise ValueError(f"non-POSIX separator: {p!r}")
    return unicodedata.normalize("NFC", p)

def path_hash(p: str) -> str:
    return hashlib.blake2b(canonical_path(p).encode("utf-8"), digest_size=8).hexdigest()

def source_token(p: str, generation: int) -> str:
    return f"n{path_hash(p)}-g{generation}.md"

def content_hash(b: bytes) -> str:
    return hashlib.blake2b(b, digest_size=16).hexdigest()

def ensure_unique_tokens(mapping: dict[str, str]) -> None:
    seen: dict[str, str] = {}
    for path, tok in mapping.items():
        if tok in seen and seen[tok] != path:
            raise TokenCollisionError(f"{tok}: {seen[tok]} vs {path}")
        seen[tok] = path
