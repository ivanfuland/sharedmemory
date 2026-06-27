# tests/test_m4_synthetic_control.py
import json


def test_shape_and_coverage():
    d = json.load(open("fixtures/m4-synthetic-control.json", encoding="utf-8"))
    assert len(d) == 15 and len([x for x in d if x["gold"] == []]) >= 2
    for x in d:
        assert x.get("split") == "synthetic" and x["span"]
        for g in x["gold"]:
            assert set(g) == {"entity", "fact"} and g["entity"] and g["fact"]
    cases = {x["span"][0]["source_path"].rsplit("/", 1)[-1] for x in d}
    for need in ("choose-a-not-b", "param-bundle", "multi-entity", "decider", "achievement-metrics"):
        assert need in cases
