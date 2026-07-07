import time
from cass_corpus.redact import redact_secrets

def test_redacts_known_secret_shapes():
    cases = [
        "sk-ant-api03-" + "A" * 30,
        "ghp_" + "B" * 36,
        "AKIA" + "1234567890ABCDEF",          # AKIA + 16
        "eyJabcdefghij.klmnopqrst.uvwxyz01234", # JWT 三段
        "CASS_MCP_BEARER=" + "a" * 64,          # 裸 64hex 赋值（关键：#1 bearer 键尾）
        "MY_TOKEN=" + "x" * 24,                 # 关键词赋值
    ]
    for c in cases:
        out = redact_secrets(f"prefix {c} suffix")
        assert "[REDACTED_SECRET]" in out, c
        # 原始密钥体不残留：对每个用例都校验密钥体尾部特征子串已消失（无 AKIA 逃逸分支）
        assert c.split("=")[-1][-20:] not in out, c

def test_idempotent():
    x = redact_secrets("token=" + "z" * 30)
    assert redact_secrets(x) == x

def test_negatives_not_redacted():
    for s in ["author = " + "y" * 24, "tokenizer_config = " + "y" * 24, "just a normal sentence"]:
        assert "[REDACTED_SECRET]" not in redact_secrets(s), s

def test_no_secret_passthrough():
    s = "hello world, this is a normal transcript line."
    assert redact_secrets(s) == s

def test_perf_gate_linear():
    big = "a" * 10240  # 10KB 全字母、无密钥
    t0 = time.perf_counter()
    redact_secrets(big)
    assert (time.perf_counter() - t0) < 0.1, "redact_secrets 必须线性（<100ms），防前导通配 O(n^2)"
