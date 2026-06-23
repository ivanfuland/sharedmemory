import json, pathlib
import pytest

CFG = pathlib.Path("config/m1-benchmarks.json")

@pytest.fixture(scope="module")
def cfg():
    assert CFG.exists(), "先跑 `uv run python benchmarks/m1_load.py`"
    return json.loads(CFG.read_text())

def test_sections_present(cfg):
    for k in ("corpus_span_days", "embedding", "distill", "derived_config"):
        assert k in cfg, f"缺段 {k}"

def test_corpus_span_and_embedding(cfg):
    assert cfg["corpus_span_days"] >= 7
    assert cfg["embedding"]["dim"] == 1536 and cfg["embedding"]["embeds_per_s"] > 0
    assert cfg["embedding"]["samples"] >= 1 and cfg["embedding"]["warmup"] >= 1

def test_distill_measured(cfg):
    assert any(v["p95_s"] > 0 for v in cfg["distill"].values())
    assert all("error_rate" in v for v in cfg["distill"].values())

def test_derived_config_complete(cfg):
    dc = cfg["derived_config"]
    for k in ("embed_batch_size", "distill_concurrency", "distill_concurrency_reason", "distill_timeout_s"):
        assert dc.get(k) is not None
