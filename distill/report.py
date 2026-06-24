# distill/report.py
import json, os, subprocess
from datetime import datetime, timezone

def build(conn, counters, backlog, deferred_reset, disc):
    qj = conn.execute("SELECT COUNT(*) FROM journal WHERE status='quarantined'").fetchone()[0]
    qr = conn.execute("SELECT COUNT(*) FROM raw_work_item WHERE status='raw_quarantined'").fetchone()[0]
    return {"ts": datetime.now(timezone.utc).isoformat(),
            "processed_count": counters.get("raw_processed", 0),   # spec §11.2 raw_processed 口径
            "new_pages": counters.get("new_pages", 0),
            "appended_entries": counters.get("appended_entries", 0),
            "raw_quarantined_count": qr, "journal_quarantined_count": qj,
            "rejected_no_provenance": counters.get("rejected_no_provenance", 0),
            "review_queued": counters.get("review_queued", 0),
            "reconciled": counters.get("reconciled", {}),
            "memory_hygiene_proposals": counters.get("memory_hygiene_proposals", 0),
            "newly_quarantined_sources": disc.get("newly_quarantined", []),
            "sync_health": {"stale_sources": [], "note": "CASS rsync 新鲜度告警在 M4/P2-tail 接入（spec §2.6.2）"},
            "total_backlog": backlog, "starved": deferred_reset.get("starved", [])}

def maybe_notify(report, cfg, _send=None):
    cap = cfg["budget"]["deferred_hard_cap"]
    anomaly = (report.get("deferred_total", 0) > cap
               or report["starved"]
               or report.get("newly_quarantined_sources")
               or report["raw_quarantined_count"] + report["journal_quarantined_count"] > 0
               or report.get("fatal"))
    if anomaly:
        msg = "蒸馏桥批次异常：\n" + json.dumps(report, ensure_ascii=False, indent=2)
        (_send or _tg_send)(msg)
    return bool(anomaly)

def _tg_send(msg):
    # opt-in 门：仅当 BRIDGE_TG_NOTIFY=1（生产 nightly 由 run-bridge.sh 设）才真发；
    # 测试/默认不发，防止测试套件（deferred-cap / crash 断点跑 run_batch）误发真 TG。
    if os.environ.get("BRIDGE_TG_NOTIFY") != "1":
        return
    # TOOLS.md fallback bot API（独立 session）；token 仅在 ~/.claude/channels/telegram/.env
    env = os.path.expanduser("~/.claude/channels/telegram/.env")
    if not os.path.exists(env): return
    tok = next((l.split("=",1)[1].strip() for l in open(env) if l.startswith("TELEGRAM_BOT_TOKEN")), None)
    if not tok: return
    subprocess.run(["curl","-s","-X","POST",f"https://api.telegram.org/bot{tok}/sendMessage",
                    "-d","chat_id=8524656058","--data-urlencode",f"text={msg[:3500]}"],
                   capture_output=True, timeout=20)
