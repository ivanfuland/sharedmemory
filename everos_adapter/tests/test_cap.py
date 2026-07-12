from everos_adapter.cap import NoopClamper, make_clamper


def test_noop_clamper_returns_content_unchanged():
    assert NoopClamper().clamp("x" * 5000, 100) == "x" * 5000


def test_noop_clamper_self_reports_as_debt():
    # 防止 NoopClamper 悄悄进 M1b
    assert NoopClamper.IS_TECHNICAL_DEBT is True


def test_make_clamper_returns_something_with_clamp():
    c = make_clamper()
    assert hasattr(c, "clamp")
    assert isinstance(c.clamp("short", 1000), str)
    assert isinstance(c.IS_TECHNICAL_DEBT, bool)


def test_make_clamper_falls_back_when_pruner_lacks_clamp(monkeypatch):
    # codex R0 P0#1：master 的 DeterministicPruner 存在但无 _clamp，
    # 只捕 ImportError 会返回一个在 clamp() 时炸 AttributeError 的 PrunerClamper。
    import cass_corpus.pruner as pm

    class _OldPruner:            # 模拟 master 上的旧版（有 _truncate_observation，无 _clamp）
        def __init__(self): pass
        def _truncate_observation(self, *a): pass

    monkeypatch.setattr(pm, "DeterministicPruner", _OldPruner)
    c = make_clamper()
    assert c.IS_TECHNICAL_DEBT is True          # 必须退化为 NoopClamper
    assert c.clamp("x" * 100, 10) == "x" * 100  # 且不抛 AttributeError
