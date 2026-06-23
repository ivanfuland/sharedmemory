import json, pathlib
import pytest

CFG = pathlib.Path("config/m2-thresholds.json")
LAB = pathlib.Path("fixtures/threshold-labeled-set.json")

def test_labeled_set_min_30():
    q = json.loads(LAB.read_text())["queries"]
    assert len(q) >= 30, f"标注集须 ≥30 条，实际 {len(q)}（§2.8）"

def test_threshold_config_shape():
    assert CFG.exists(), "先跑 hooks/calibrate_threshold.py"
    c = json.loads(CFG.read_text())
    assert 0.0 < c["query_threshold"] <= 1.0
    for k in ("status", "method", "labeled_n", "positive_labels", "recalibrate_after", "sweep"):
        assert k in c, f"缺 {k}"
    assert c["recalibrate_after"] == "P4", "须显式声明 P4 重标"
    assert c["status"] in ("calibrated", "calibrated_low_precision", "uncalibrated_default")

def test_no_fake_calibration_on_empty_brain():
    """内容不足时必须诚实标 uncalibrated_default + 退保守默认，不得假装已标定（codex R1 #11）。"""
    import sys, pathlib as _p
    sys.path.insert(0, str(_p.Path("hooks").resolve()))
    import gbrain_digest as gd
    c = json.loads(CFG.read_text())
    if c["status"] == "uncalibrated_default":
        assert c["query_threshold"] == gd.DEFAULT_THRESHOLD, "uncalibrated 必须退保守默认"
        assert c["positive_labels"] < 5 or c["max_tp"] == 0, "uncalibrated 须因内容不足"
