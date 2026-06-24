"""Task 11: quality_eval.py 单测（mock chat + mock judge，零 LLM 调用）"""
from distill import quality_eval

EVAL = [{"span": [{"idx": 0, "role": "user",
                   "content": "老兰决定 LFT 用 Qlib 做底座",
                   "source_path": "/p"}],
          "gold": [{"entity": "LFT", "fact": "用 Qlib 做底座"}]}]


def _cfg():
    return {
        "distill": {"base_url": "x", "api_key": "x", "model": "gpt-5.4-mini"},
        "budget": {"chunk_char_size": 24000, "chunk_overlap": 400},
        "derived": {"distill_timeout_s": 90},
        "paths": {"audit_log": "/tmp/cc-m3-q.log"},
    }


def test_evaluate_perfect():
    def chat(b, c):
        return {"candidates": [{"entity_name": "LFT", "entity_kind": "project",
                                "entry_type": "decision", "fact_text": "用 Qlib 做底座",
                                "source_idx": 0}]}

    def judge(gold, ext, cfg, chat):
        return len(gold)  # 全匹配

    m = quality_eval.evaluate(_cfg(), EVAL, _chat=chat, _judge=judge)
    assert m["precision"] == 1.0 and m["recall"] == 1.0
    assert quality_eval.gate(m) is True


def test_gate_fails_low_recall():
    assert quality_eval.gate({"precision": 1.0, "recall": 0.5, "f1": 0.6}) is False
    assert quality_eval.gate({"precision": 0.8, "recall": 0.9, "f1": 0.85}) is False  # P<0.9


def test_lock_model_writes_config(tmp_path):
    import json
    p = tmp_path / "m3.json"
    p.write_text('{"model_lock":{"status":"pending_quality_gate"},"budget":{}}', encoding="utf-8")
    quality_eval.lock_model("gpt-5.4-mini", {"precision": 0.95, "recall": 0.85, "f1": 0.9}, str(p))
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["model_lock"]["status"] == "locked" and d["model_lock"]["model"] == "gpt-5.4-mini"
