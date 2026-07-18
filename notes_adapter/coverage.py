import re, unicodedata
from collections import Counter

_SEP = re.compile(r"^[-*=]{3,}$")   # P2-2：与 everalgo _postprocess 一致（不含 _）；`___` fixture 防漂移

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", s))

def char_multiset_recall(source: str, topic_contents, splitter=None) -> float:
    if splitter is None:
        from everalgo.knowledge._block_split import split_content_to_blocks as splitter  # 探针环境
    atoms = [t for _, t in splitter(source) if t.strip() and not _SEP.match(t.strip())]
    src = Counter(_norm("".join(atoms)))
    if not src: return 1.0
    have = Counter(_norm("".join(topic_contents)))
    covered = sum(min(n, have[c]) for c, n in src.items())
    return covered / sum(src.values())
