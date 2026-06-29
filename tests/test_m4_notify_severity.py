# tests/test_m4_notify_severity.py
# maybe_notify 告警分级（M4）：只有「需立即人工介入」的真异常发 TG；
# 稳态积压(starved) + 既有累计 quarantine 不发 TG（防 bulk-drain 阶段每批轰炸）。
from distill import report

CFG = {"budget": {"deferred_hard_cap": 2000}}


def _clean_report(**over):
    """一个干净 batch 的 report：无任何告警条件。测试按需覆盖单个字段。"""
    rep = {
        "deferred_total": 100,
        "starved": [],
        "newly_quarantined_sources": [],
        "raw_quarantined_count": 0,
        "journal_quarantined_count": 0,
        "raw_quarantined_new": 0,
        "reconciled": {"quarantined": 0, "review": 0},
        "fatal": None,
    }
    rep.update(over)
    return rep


def _run(rep):
    sent = []
    notified = report.maybe_notify(rep, CFG, _send=lambda m: sent.append(m))
    return notified, sent


# ---- 不发 TG：稳态积压健康（drain 阶段每批都会出现，不该轰炸）----

def test_starved_only_does_not_send_tg():
    notified, sent = _run(_clean_report(starved=[1, 2, 3, 4, 5]))
    assert sent == []
    assert notified is False


def test_preexisting_cumulative_quarantine_does_not_send_tg():
    # 既有累计 quarantine（之前批次卡住的）非本批新增 → 不重复告警
    notified, sent = _run(_clean_report(raw_quarantined_count=4, journal_quarantined_count=2))
    assert sent == []
    assert notified is False


def test_clean_batch_does_not_send_tg():
    notified, sent = _run(_clean_report())
    assert sent == []
    assert notified is False


# ---- 发 TG：需立即人工介入 ----

def test_new_raw_quarantine_this_batch_sends_tg():
    notified, sent = _run(_clean_report(raw_quarantined_new=1, raw_quarantined_count=5))
    assert len(sent) == 1
    assert notified is True


def test_new_journal_quarantine_this_batch_sends_tg():
    notified, sent = _run(_clean_report(reconciled={"quarantined": 1, "review": 0}))
    assert len(sent) == 1
    assert notified is True


def test_new_journal_review_this_batch_sends_tg():
    # 歧义实体命中 → 本批需人工消歧
    notified, sent = _run(_clean_report(reconciled={"quarantined": 0, "review": 1}))
    assert len(sent) == 1
    assert notified is True


def test_deferred_total_over_cap_sends_tg():
    notified, sent = _run(_clean_report(deferred_total=2001))
    assert len(sent) == 1
    assert notified is True


def test_fatal_sends_tg():
    notified, sent = _run(_clean_report(fatal="deferred_total_exceeds_hard_cap"))
    assert len(sent) == 1
    assert notified is True


def test_newly_quarantined_sources_sends_tg():
    notified, sent = _run(_clean_report(newly_quarantined_sources=["unknown-agent"]))
    assert len(sent) == 1
    assert notified is True


# ---- 混合：稳态噪音 + 本批真异常 → 仍发（真异常优先）----

def test_starved_plus_new_quarantine_still_sends():
    notified, sent = _run(_clean_report(starved=[1, 2, 3], raw_quarantined_new=2))
    assert len(sent) == 1
    assert notified is True
