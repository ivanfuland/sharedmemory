from everos_eval.queryset import Candidate, select_candidates, raw_baseline

CUT = 1_000_000  # 测试用 cutoff(epoch ms 语义;CASS created_at 是毫秒 epoch 整数,禁用字符串比较——codex R1)


def _c(eid, ts_ms):
    return Candidate(eid, 1, "claude_code", 7, ts_ms)


def test_select_prefers_post_cutoff_and_is_deterministic():
    cands = [_c(f"e{i}", CUT + 1 + i) for i in range(40)]
    sel, tier = select_candidates(cands, snapshot_eids=set(), cutoff_ms=CUT, target=30)
    assert tier == "post_cutoff" and len(sel) == 30
    sel2, _ = select_candidates(cands, set(), CUT, 30)
    assert [c.external_id for c in sel] == [c.external_id for c in sel2]  # stable_hash 可复现


def test_select_widens_when_insufficient_and_excludes_snapshot():
    newer = [_c("new1", CUT + 1)]
    older = [_c(f"old{i}", CUT - 100) for i in range(35)] + [_c("snap1", CUT - 100)]
    sel, tier = select_candidates(newer + older, snapshot_eids={"snap1"}, cutoff_ms=CUT, target=30)
    assert tier == "widened_non_snapshot" and len(sel) == 30
    assert all(c.external_id != "snap1" for c in sel)


def test_raw_baseline_caps_at_500():
    assert len(raw_baseline(["甲" * 400, "乙" * 400])) == 500
