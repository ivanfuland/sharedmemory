# distill/reconcile.py
from distill import writer, state, idempotency


def timeline_has_key(cfg, token, slug, key, _call=None):
    """读 slug 的完整 timeline，扫描是否含 [dk:<hash>] marker（spec §2.5.1 class 2/3）。
    读失败/页不存在 → 返回 False（走正常写路径）。

    get_timeline 返回真实 gbrain 形态：list of {id,page_id,date,summary,detail,...}。
    扫描每个 entry 的 summary + detail 字段。
    """
    call = _call or writer.mcp_call
    try:
        out = call(cfg, token, "get_timeline", {"slug": slug})
    except Exception:
        return False   # 页不存在/读失败 → 视为未落，走写路径
    marker = idempotency.key_marker(key)
    if isinstance(out, list):
        return any(marker in (entry.get("summary", "") + entry.get("detail", ""))
                   for entry in out if isinstance(entry, dict))
    return False


def reconcile_pending(cfg, token, conn, max_entities=None, _call=None):
    """统一写+崩溃恢复路径（codex R0 P0-4）：
    每条 pending 先 timeline 扫 key →
      · 已落：标 done（零重复，不占写预算）
      · 超 max_entities 写预算：journal deferred（codex R1 P0-3）
      · PreWriteError（search/get 写前失败）：留 pending 重试（R4 P1-1）
      · write_entry 成功：appended（new_pages 由 done_new 派生）
      · McpError / OSError / ValueError（写后故障）：quarantined（spec §2.6.1, R3 P1-3）
      · review_queued（歧义命中）：quarantined（防每批重入队无界积压，R2 P0-3）
    """
    res = {"already": 0, "new_pages": 0, "appended": 0,
           "review": 0, "quarantined": 0, "deferred": 0, "retry_later": 0}
    written = 0
    rows = conn.execute(
        "SELECT key, raw_work_item_id, entity_slug, entry_type, fact_text, source_ref, entry_date"
        " FROM journal WHERE status='pending'"
    ).fetchall()
    for row in rows:
        jr = dict(row)
        # 崩溃恢复：key 已在 timeline → 标 done，不占写预算，不重写
        if timeline_has_key(cfg, token, jr["entity_slug"], jr["key"], _call=_call):
            conn.execute("UPDATE journal SET status='done' WHERE key=? AND status='pending'", (jr["key"],))
            conn.commit()
            res["already"] += 1
            continue
        # 实体写预算耗尽 → 次日（spec §2.6.1）
        if max_entities is not None and written >= max_entities:
            conn.execute("UPDATE journal SET status='deferred' WHERE key=? AND status='pending'", (jr["key"],))
            conn.commit()
            res["deferred"] += 1
            continue
        # 尝试写入
        try:
            r = writer.write_entry(cfg, token, conn, jr, _call=_call)
        except writer.PreWriteError:
            # 写前失败（search/get 瞬时网络抖动）→ 留 pending，下批重试（R4 P1-1）
            res["retry_later"] += 1
            continue
        except (writer.McpError, OSError, ValueError):
            # 写后故障 → quarantine
            conn.execute("UPDATE journal SET status='quarantined' WHERE key=? AND status='pending'", (jr["key"],))
            conn.commit()
            res["quarantined"] += 1
            continue
        if r == "review_queued":
            # 歧义出 pending → quarantine（防每批重入队无界积压，R2 P0-3）
            conn.execute("UPDATE journal SET status='quarantined' WHERE key=? AND status='pending'", (jr["key"],))
            conn.commit()
            res["review"] += 1
            continue
        written += 1
        if r == "done_new":
            res["new_pages"] += 1
        res["appended"] += 1
    return res


def replay_quarantined(conn, keys=None, raw_ids=None):
    """replay quarantined entries → pending（调 state.replay_*，affected==1 断言由 state 保证）。"""
    out = {"journal": 0, "raw": 0}
    for k in (keys or []):
        state.replay_journal(conn, k)
        out["journal"] += 1
    for rid in (raw_ids or []):
        state.replay_raw(conn, rid)
        out["raw"] += 1
    return out
