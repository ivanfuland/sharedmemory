from distill import secret_scan
def test_catches_real_secrets():
    assert "api_key" in secret_scan.scan_span({"span":[{"content":"key=sk-bd3b3c6c961142159c5e2f84dfb721b9"}]})
    assert "ctx_hex_secret" in secret_scan.scan_span({"span":[{"content":"token = a1b2c3d4e5f60718293a4b5c6d7e8f90"}]})
def test_no_false_positive_on_git_sha():
    # 裸 40-hex（git SHA）不该命中
    assert secret_scan.scan_span({"span":[{"content":"commit 0a1b2c3d4e5f60718293a4b5c6d7e8f90123456 修复了 bug"}]})==[]
    assert secret_scan.scan_span({"span":[{"content":"老兰决定 LFT 用 Qlib"}]})==[]
