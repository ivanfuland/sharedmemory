import pytest
from everos_mcp import contract

def test_linebreak_checked_before_strip():
    # R7 抓过的绕过:尾随 \n 若先 strip 会被洗掉——必须先查原始输入
    with pytest.raises(contract.ContractError) as e:
        contract.validate_task("fix inngest retry\n")
    assert e.value.code == "task_has_linebreak"

@pytest.mark.parametrize("ch", ["\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\u2028", "\u2029", "\x85"])
def test_all_splitlines_boundaries_rejected(ch):
    with pytest.raises(contract.ContractError):
        contract.validate_task(f"a{ch}b")

def test_strip_then_empty_and_length():
    with pytest.raises(contract.ContractError) as e:
        contract.validate_task("   ")
    assert e.value.code == "task_empty"
    assert contract.validate_task(" " + "x"*150 + " ") == "x"*150  # 恰好 150 过
    with pytest.raises(contract.ContractError) as e:
        contract.validate_task("x"*151)
    assert e.value.code == "task_too_long"

@pytest.mark.parametrize("v", [0, -1, 6])
def test_limit_domain(v):
    with pytest.raises(contract.ContractError):
        contract.validate_limit(v)

def test_clamp_payload_deterministic_and_whitelist():
    p = {"task_intent":"a"*5000, "approach":"b"*5000, "key_insight":"c"*100, "junk":"x"}
    out, truncated = contract.clamp_payload(p, "agent_case")
    assert truncated and "junk" not in out
    assert sum(len(v) for v in out.values()) <= 8000
    out2, _ = contract.clamp_payload(p, "agent_case")
    assert out == out2  # 同输入必同输出(payload_sha 跨实现稳定)

def test_clamp_none_counts_zero():
    p = {"task_intent":"a"*9000, "approach":None, "key_insight":"c"}
    out, truncated = contract.clamp_payload(p, "agent_case")
    assert truncated and out["approach"] is None
