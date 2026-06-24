# distill/run.py
import os, sys
from datetime import datetime, timezone
from distill import config, state, cass_reader, filters, distiller, writer, reconcile, report, memory_hygiene

REQUIRED_TOOLS = ("put_page", "add_timeline_entry", "search", "get_timeline", "get_page")

def _deferred_total(conn):
    return (conn.execute("SELECT COUNT(*) FROM raw_work_item WHERE status='raw_deferred'").fetchone()[0]
            + conn.execute("SELECT COUNT(*) FROM journal WHERE status='deferred'").fetchone()[0])

def run_batch(cfg, sources=None, today=None, allow_unreleased=False, _chat=None, _call=None):
    today = today or datetime.now(timezone.utc).date().isoformat()
    b = cfg["budget"]
    with state.flock_lease(cfg["paths"]["lock"]):
        cass_reader.verify_fingerprint(cfg["paths"]["canon_db"], cfg["paths"]["fingerprint"])  # 不匹配 raise（fatal）
        conn = state.connect(cfg["paths"]["state_db"])
        token = writer.load_token(cfg)
        tools = set(REQUIRED_TOOLS) if _call is not None else writer.probe_tools(cfg, token)
        missing = [t for t in REQUIRED_TOOLS if t not in tools]
        assert not missing, f"gbrain MCP 缺工具 {missing}（probe={tools}）"
        counters = {"raw_processed": 0, "rejected_no_provenance": 0}
        rd = state.reset_deferred(conn, today)                       # 0) deferred 复位（优先处理）
        disc = cass_reader.discover_sources(cfg["paths"]["canon_db"], conn)   # 1) 发现 + 未知 quarantine（P0-2/R1 P0-1）
        if sources is None:
            use_sources = disc["to_process"]
        elif allow_unreleased:
            use_sources = sources
        else:                                                        # explicit sources 也须 known/released，按 (source_id, workspace) 双键（R3 P1-4）
            ok = {(sid, ws) for sid, ag, ws in disc["to_process"]}
            use_sources = [src for src in sources if (src[0], src[2] if len(src) > 2 else "") in ok]
        for src in use_sources:                                      # 2) read phase（src=(source_id,agent[,workspace])，R2 P0-2）
            source_id, agent = src[0], src[1]
            workspace = src[2] if len(src) > 2 else ""
            cass_reader.read_spans(cfg["paths"]["canon_db"], conn, source_id, agent, b["max_entities"] * 4, workspace)
        tokens_used = 0                                              # 3) distill phase（token 预算 R1 P0-3）
        for raw in conn.execute("SELECT id,conversation_id,span_start,span_end,session_ref FROM raw_work_item"
                                " WHERE status='new' ORDER BY id").fetchall():
            if tokens_used >= b["batch_token_cap"]:
                conn.execute("UPDATE raw_work_item SET status='raw_deferred' WHERE id=? AND status='new'", (raw["id"],))
                conn.commit(); continue                             # 已超 → 不读
            rows = cass_reader.read_span_messages(cfg["paths"]["canon_db"], raw["conversation_id"],
                                                  raw["span_start"], raw["span_end"])
            kept, _ = filters.filter_span_messages(rows)
            if not kept:
                conn.execute("UPDATE raw_work_item SET status='distilled' WHERE id=?", (raw["id"],)); conn.commit()
                counters["raw_processed"] += 1; continue            # noise-only 也计入 processed（R1 P1-3）
            delta = sum(len(r.get("content", "")) for r in kept) // 3   # 粗估 token（中文~1.5/英文~4 char/tok；//3 保守安全垫，非精确）
            if tokens_used and tokens_used + delta > b["batch_token_cap"]:
                conn.execute("UPDATE raw_work_item SET status='raw_deferred' WHERE id=? AND status='new'", (raw["id"],))
                conn.commit(); continue                             # 加本 span 会超 → defer；首 span(tokens_used=0)例外不饿死（R4 P1-3）
            tokens_used += delta
            try:
                out = distiller.distill_span(kept, cfg, _chat=_chat)
            except Exception:
                conn.execute("UPDATE raw_work_item SET status='raw_quarantined' WHERE id=? AND status='new'", (raw["id"],))
                conn.commit(); continue                             # retry×2 仍败 → raw_quarantined
            counters["rejected_no_provenance"] += out["rejected_no_provenance"]
            sp = kept[0].get("source_path", raw["session_ref"])
            distiller.commit_distilled(conn, raw["id"], out["candidates"], sp)   # 单事务 build+insert+mark
            counters["raw_processed"] += 1
        rec = reconcile.reconcile_pending(cfg, token, conn, max_entities=b["max_entities"], _call=_call)  # 4) 写(实体预算)+恢复
        counters.update({"new_pages": rec["new_pages"], "appended_entries": rec["appended"],
                         "review_queued": rec["review"], "reconciled": rec})
        mem_md = cfg["paths"].get("memory_md")                       # 5) MEMORY.md 卫生 dry-run（fail-soft，R1 P1-4）
        if mem_md and os.path.exists(mem_md):
            try:
                hyg = memory_hygiene.analyze(cfg, token, mem_md, cfg["paths"]["hygiene_out"], _call=_call)
                counters["memory_hygiene_proposals"] = hyg.get("proposals", 0)
            except Exception:
                counters["memory_hygiene_proposals"] = -1
        backlog = state.total_backlog(conn)                          # 6) 报告 + deferred 总量硬上限闸（R1 P0-2）
        rep = report.build(conn, counters, backlog, rd, disc)
        rep["deferred_total"] = _deferred_total(conn)
        if rep["deferred_total"] > b["deferred_hard_cap"]:
            rep["fatal"] = "deferred_total_exceeds_hard_cap"
            report.maybe_notify(rep, cfg)
            raise SystemExit(f"deferred 总量 {rep['deferred_total']} 超硬上限 {b['deferred_hard_cap']}，停桥待人工")
        report.maybe_notify(rep, cfg)
        return rep

def main():
    cfg = config.load()
    rep = run_batch(cfg)                                             # 来源由 discover 自动发现（codex R0 P0-2）
    import json; print(json.dumps(rep, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
