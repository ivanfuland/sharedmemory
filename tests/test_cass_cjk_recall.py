"""CASS 中英文召回 baseline（固定 query set + ground-truth，可复现）。
cass 缺失 fail 不 skip。中文召回实测良好（baseline build 词法对 CJK 可用）。"""
import json
import os
import subprocess
import pathlib
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
QS = REPO / "fixtures" / "cjk-recall-queryset.json"
DATA_DIR = os.environ.get("CASS_DATA_DIR", os.path.expanduser("~/.local/share/coding-agent-search"))
CASS = os.environ.get("CASS_BIN", "cass")


def _require_cass():
    if subprocess.run(["which", CASS], capture_output=True).returncode != 0:
        pytest.fail("cass 不在 PATH——召回 baseline 必跑，先完成 Task 2")


def _search(q):
    r = subprocess.run([CASS, "search", q, "--robot", "--mode", "lexical",
                        "--limit", "5", "--data-dir", DATA_DIR],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return []
    return json.loads(r.stdout).get("hits", [])


@pytest.mark.parametrize("case", json.loads(QS.read_text()))
def test_recall(case):
    _require_cass()
    gt = case["ground_truth_token"]
    assert not gt.startswith("<"), f"{case['q']} 的 ground_truth_token 仍是占位"
    hits = _search(case["q"])
    blob = json.dumps(hits, ensure_ascii=False)
    gt_present = gt in blob
    # en 与 zh 均硬要求命中（实测 CJK 词法在 baseline build 可用，故中文也硬验）
    assert len(hits) >= case["min_hits"], f"{case['lang']} query {case['q']} 命中不足"
    assert gt_present, f"{case['lang']} query {case['q']} 结果未含 ground-truth {gt}"
