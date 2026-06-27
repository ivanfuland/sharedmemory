"""Task 2: bootstrap CI + 配对非劣 + 欠功效门 + split 报分 测试。"""
from distill import quality_eval as qe


def test_flat_ci_degenerate():
    per = [{"gold": 1, "extracted": 1, "matched": 1, "cluster": "c"} for _ in range(10)]
    ci = qe.bootstrap_ci(per, n_boot=500, seed=1)
    assert ci["p_lo"] == 1.0 and ci["r_lo"] == 1.0


def test_flat_ci_reproducible():
    per = [{"gold": 2, "extracted": 3, "matched": 1} for _ in range(16)]
    assert qe.bootstrap_ci(per, 500, 42) == qe.bootstrap_ci(per, 500, 42)


def test_clustered_ci_records_n_clusters():
    per = [{"gold": 1, "extracted": 1, "matched": 1, "cluster": a} for a in ("x", "y", "z") for _ in range(4)]
    ci = qe.bootstrap_ci_clustered(per, n_boot=300, seed=3)
    assert ci["n_clusters"] == 3


def test_paired_delta_identical_models_zero_delta():
    per = [{"source": f"/s/{i}", "gold": 2, "extracted": 2, "matched": 1} for i in range(12)]
    d = qe.paired_delta_ci(per, per, n_boot=400, seed=2)
    assert d["dp_lo"] == 0.0 and d["dr_hi"] == 0.0 and d["n_paired"] == 12   # flash==mini → 差值恒 0


def test_gate_paired_and_power():
    d = {"dp_lo": -0.02, "dr_lo": -0.01}
    ci = {"p_lo": 0.9, "r_lo": 0.82}
    assert qe.gate_paired(d, ci) is True                                 # 差值在 -margin 内 + 过地板
    assert qe.gate_paired({"dp_lo": -0.2, "dr_lo": 0.0}, ci) is False   # flash 比 mini 差太多
    assert qe.gate_paired(d, {"p_lo": 0.80, "r_lo": 0.82}) is False     # 不过绝对地板
    assert qe.power_ok({"p_lo": 0.9, "p_hi": 0.95, "r_lo": 0.85, "r_hi": 0.9, "n_clusters": 5}) is True
    assert qe.power_ok({"p_lo": 0.6, "p_hi": 0.99, "r_lo": 0.6, "r_hi": 0.99, "n_clusters": 2}) is False  # 簇少+宽
