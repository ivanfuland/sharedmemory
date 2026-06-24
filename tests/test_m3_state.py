import sqlite3, pytest
from distill import state

def _c(tmp_path): return state.connect(str(tmp_path / "s.db"))

def test_schema_and_backlog_counts_read_after_crash_visible(tmp_path):
    c = _c(tmp_path)
    # read 提交后崩溃模拟：raw=new、journal 空 → 必须进 backlog（spec §2.6.1 total_backlog 定义）
    c.execute("INSERT INTO raw_work_item(source_id,conversation_id,span_start,span_end,session_ref,status,created_at)"
              " VALUES('ubuntu-cc',1,1,9,'sess:1','new','2026-06-24')")
    c.commit()
    b = state.total_backlog(c)
    assert b == {"raw_backlog": 1, "journal_backlog": 0, "total_backlog": 1}

def test_raw_span_unique_reread_is_noop(tmp_path):
    c = _c(tmp_path)
    sql = ("INSERT OR IGNORE INTO raw_work_item(source_id,conversation_id,span_start,span_end,session_ref,status,created_at)"
           " VALUES('ubuntu-cc',1,1,9,'sess:1','new','2026-06-24')")
    c.execute(sql); c.execute(sql); c.commit()  # 重读同 span
    assert c.execute("SELECT COUNT(*) FROM raw_work_item").fetchone()[0] == 1

def test_replay_journal_asserts_affected_one(tmp_path):
    c = _c(tmp_path)
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,created_at)"
              " VALUES('k1',1,'people/张三','fact','f','sess:1#1-9','2026-06-24','quarantined','2026-06-24')")
    c.commit()
    state.replay_journal(c, "k1")
    assert c.execute("SELECT status FROM journal WHERE key='k1'").fetchone()[0] == "pending"

def test_replay_zero_rows_fail_loud_and_ledgered(tmp_path):
    c = _c(tmp_path)  # key 不存在 → affected==0
    with pytest.raises(state.ReplayError):
        state.replay_journal(c, "nonexistent")
    assert c.execute("SELECT affected,layer FROM replay_ledger").fetchone()["affected"] == 0

def test_flock_second_instance_exits(tmp_path):
    lock = str(tmp_path / "bridge.lock")
    with state.flock_lease(lock):
        with pytest.raises(SystemExit):
            with state.flock_lease(lock):
                pass

def test_reset_deferred_starvation_after_two_days(tmp_path):
    c = _c(tmp_path)
    c.execute("INSERT INTO journal(key,raw_work_item_id,entity_slug,entry_type,fact_text,source_ref,entry_date,status,deferred_days,created_at)"
              " VALUES('k2',1,'p/x','fact','f','r','2026-06-24','deferred',1,'2026-06-24')")
    c.commit()
    r = state.reset_deferred(c, "2026-06-25")
    assert c.execute("SELECT status,deferred_days FROM journal WHERE key='k2'").fetchone()["status"] == "pending"
    assert "k2" in r["starved"]  # 连续 2 天 deferred → 饥饿告警
