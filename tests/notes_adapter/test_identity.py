import unicodedata, pytest
from notes_adapter import identity as I

def test_canonical_path_nfc_and_posix():
    nfd = unicodedata.normalize("NFD", "复盘/记忆层.md")
    assert I.canonical_path(nfd) == unicodedata.normalize("NFC", "复盘/记忆层.md")
    with pytest.raises(ValueError):
        I.canonical_path("a\\b.md")   # P2-1：反斜杠拒收（POSIX 契约,fail-loud）

def test_source_token_format_and_generation():
    t = I.source_token("801/x.md", 3)
    assert t.startswith("n") and t.endswith("-g3.md") and len(t.split("-g")[0]) == 17
    assert I.source_token("801/x.md", 4) != t          # 代际不同 token 不同
    assert "/" not in t

def test_content_hash_len():
    assert len(I.content_hash(b"abc")) == 32

def test_token_collision_fail_loud():
    with pytest.raises(I.TokenCollisionError):
        I.ensure_unique_tokens({"a.md": "nXX-g1.md", "b.md": "nXX-g1.md"})
