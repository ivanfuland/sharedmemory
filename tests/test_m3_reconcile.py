from distill import reconcile, writer, state, idempotency
from tests.test_m3_writer import FakeGbrain, _jrow, _cfg


def _seed_pending(c, jr):
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES(?,1,?,?,?,?,?,'pending','2026-06-24')",
              (jr["key"], jr["entity_slug"], jr["entry_type"], jr["fact_text"], jr["source_ref"], jr["entry_date"]))
    c.commit()


def test_reconcile_already_landed_marks_done_no_dup(tmp_path):
    # 模拟崩溃：gbrain 已写（含 key marker）但 journal 还 pending（写后、标 done 前崩）
    fake = FakeGbrain()
    c = state.connect(str(tmp_path / "s.db"))
    jr = _jrow()
    _seed_pending(c, jr)
    fake.pages[jr["entity_slug"]] = "x"
    fake.timelines[jr["entity_slug"]] = [{"date": "2026-06-24", "summary": f'已落 {idempotency.key_marker(jr["key"])}'}]
    r = reconcile.reconcile_pending(_cfg(tmp_path), "tok", c, _call=fake)
    assert r["already"] == 1 and r["appended"] == 0
    assert c.execute("SELECT status FROM journal WHERE key=?", (jr["key"],)).fetchone()[0] == "done"
    # 关键：未新增重复 timeline 条目（仍 1 条）
    assert len(fake.timelines[jr["entity_slug"]]) == 1


def test_reconcile_not_landed_rewrites(tmp_path):
    fake = FakeGbrain()
    c = state.connect(str(tmp_path / "s.db"))
    jr = _jrow()
    _seed_pending(c, jr)
    r = reconcile.reconcile_pending(_cfg(tmp_path), "tok", c, _call=fake)
    assert r["appended"] == 1
    assert c.execute("SELECT status FROM journal WHERE key=?", (jr["key"],)).fetchone()[0] == "done"


def test_replay_quarantined_journal(tmp_path):
    c = state.connect(str(tmp_path / "s.db"))
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES('kq',1,'p/x','fact','f','r','2026-06-24','quarantined','2026-06-24')")
    c.commit()
    reconcile.replay_quarantined(c, keys=["kq"])
    assert c.execute("SELECT status FROM journal WHERE key='kq'").fetchone()[0] == "pending"


def test_prewrite_failure_stays_pending(tmp_path):
    c = state.connect(str(tmp_path / "s.db"))
    jr = _jrow()
    _seed_pending(c, jr)

    def flaky(cfg, token, tool, args):
        if tool == "search":
            raise writer.McpError("network blip")   # 写前阶段瞬时失败
        return {}

    res = reconcile.reconcile_pending(_cfg(tmp_path), "tok", c, _call=flaky)
    assert res["retry_later"] == 1
    assert c.execute("SELECT status FROM journal WHERE key=?", (jr["key"],)).fetchone()[0] == "pending"   # 留 pending 重试，不 quarantine（R4 P1-1）
