from notes_adapter.digest import canonical_digest
import hashlib

def test_formula_frozen_no_trailing_lf():
    fields = {"frontmatter:summary": "s", "frontmatter:topic_name": "n",
              "frontmatter:topic_path": "p", "frontmatter:category_id": "c",
              "frontmatter:depth": "1", "body": "b"}
    expect = hashlib.sha256("\n".join(f"{k}={v}" for k in sorted(fields) for v in [fields[k]]).encode()).hexdigest()
    assert canonical_digest("s", "n", "p", "c", 1, "b\n") == expect   # content.rstrip()

import pytest
@pytest.mark.parametrize("mut", [
    {"summary": "s2"}, {"topic_path": "p2"}, {"depth": 9},   # 单腿漂移三例
])
def test_verify_rows_detects_single_leg_drift(mut):
    from notes_adapter.digest import TopicRow, verify_rows, canonical_digest as cd
    base = dict(node_id="n1", summary="s", topic_name="n", topic_path="p",
                category_id="c", depth=1, content="b")
    good_digest = cd("s", "n", "p", "c", 1, "b")
    md = "---\nsummary: s\ntopic_name: n\ntopic_path: p\ncategory_id: c\ndepth: 1\n---\nb"
    drifted = {**base, **mut}
    bad = verify_rows([TopicRow(**drifted, content_sha256="")], {"n1": good_digest}, {"n1": md})
    assert bad, f"drift {mut} not detected"
