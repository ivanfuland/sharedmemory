"""M1b probe 批量喂料脚本（操作性，Phase B Task 7 唯一入口，不进 pytest）。

串行读快照 -> 逐会话 run_session -> 轮询 markdown 终态 -> 日志窗口分类 -> 记 outcome ->
查 LiteLLM spend、接近 cap 就停。断点续跑：已记录过的 external_id 跳过，脚本可中断重跑。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time

import httpx

from everos_adapter.cap import make_clamper
from everos_adapter.pipeline import run_session
from everos_adapter.scan_terminal import find_session_case_files, session_case_entry_ids
from everos_probe.attribution import classify_session, read_log_window

AGENT_ID = "everos-m1b-probe"
USER_SENDER = "demo-user"


def _spend(admin_base: str, admin_key: str, target_key: str) -> float:
    r = httpx.get(
        f"{admin_base.rstrip('/')}/key/info",
        params={"key": target_key},
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=10,
    )
    r.raise_for_status()
    info = r.json()
    d = info.get("info", info)
    return float(d.get("spend") or 0.0)


# EverOS 的 agent_case 抽取是 /flush 之后的异步 job(大会话 >60s 才落日志)。判定太早
# 会把还没跑完的会话误判成 other。这些正则标志「该会话的 agent_case 已出终态」:
# 除 case 产出(pass)外,任一 reject/skip 落日志即视为可判定。
_AGENT_CASE_TERMINAL = re.compile(
    r"agent_case_skipped_by_algo|agent_case_skipped_no_assistant"
    r"|filtered out by LLM|skipping memcell"
    r"|LLM returned empty '(?:task_intent|approach)'"
)


def _short_sid(external_id: str) -> str:
    """EverOS /memory/add 限制 session_id ≤128 字符,而 CASS external_id 是文件路径,
    subagent/workflow 深路径会超(实测 139 字符 → 422 INVALID_INPUT)。用稳定哈希短 id
    喂 EverOS(日志绑卡都用它),outcomes 仍记原始 external_id。"""
    return "m1b-" + hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:40]


def _collect_case_ids(memory_root: str, session_id: str) -> list:
    ids = []
    for f in find_session_case_files(memory_root, session_id):
        ids.extend(session_case_entry_ids(f.read_text(encoding="utf-8"), session_id))
    return ids


def _wait_terminal(memory_root: str, session_id: str, log_path: str, start_offset: int,
                   timeout_s: int = 240, poll_s: int = 5) -> list:
    """轮询直到该会话有明确终态——case 产出(pass)或日志窗口出现 agent_case 终态信号
    (reject/skip)。EverOS 的 agent_case 抽取是 /flush 之后的异步 job,大会话要 >60s;
    死等固定 60s 会在 job 没跑完时误判 other(实测:上一轮 20 个 other 里 15 个判早了)。
    这里等到真正终态才返回,上限 timeout_s 兜底(仍未出终态才算超时,交 classify 判)。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ids = _collect_case_ids(memory_root, session_id)
        if ids:
            return ids
        end = os.path.getsize(log_path) if os.path.exists(log_path) else start_offset
        window = read_log_window(log_path, start_offset, end) if os.path.exists(log_path) else ""
        if _AGENT_CASE_TERMINAL.search(window):
            return ids   # reject/skip 终态已落日志(ids 空),交 classify_session 判具体类别
        time.sleep(poll_s)
    return _collect_case_ids(memory_root, session_id)


def run(snapshot_path, base_url, memory_root, log_path, outcomes_path,
        admin_base, admin_key, target_key, cap_usd=10.0, stop_margin_usd=0.5,
        limit=None) -> None:
    from everos_probe.sampling import load_snapshot

    manifest = load_snapshot(snapshot_path)
    outcomes = []
    if os.path.exists(outcomes_path):
        with open(outcomes_path, encoding="utf-8") as f:
            outcomes = [json.loads(line) for line in f if line.strip()]
    done_eids = {o["external_id"] for o in outcomes}
    processed = 0

    with open(outcomes_path, "a", encoding="utf-8") as out_f:
        for stratum, members in manifest["strata"].items():
            for m in members:
                eid = m["external_id"]
                if eid in done_eids:
                    continue
                if limit is not None and processed >= limit:
                    print(f"reached --limit {limit}", file=sys.stderr)
                    return

                spend = _spend(admin_base, admin_key, target_key)
                if spend >= cap_usd - stop_margin_usd:
                    print(f"STOP near cap: spend={spend} cap={cap_usd}", file=sys.stderr)
                    return
                processed += 1
                sid = _short_sid(eid)   # ≤128 字符喂 EverOS;outcomes 仍记原始 eid

                start_offset = os.path.getsize(log_path) if os.path.exists(log_path) else 0
                # 422 的真根因是并发(EverOS 后台 job 忙时撞喂料),非 payload。单独喂/延迟喂
                # 都成功——所以递增延迟重试,给 EverOS 时间排空后台 job 再试。
                result = None
                last_err = None
                for attempt in range(4):
                    try:
                        result = run_session(base_url, sid, m["rows"], AGENT_ID, USER_SENDER,
                                              clamper=make_clamper())
                        break
                    except httpx.HTTPError as e:
                        last_err = e
                        if attempt < 3:
                            time.sleep(15 * (attempt + 1))   # 15/30/45s 递增退避
                if result is None:
                    record = {"external_id": eid, "stratum": stratum,
                              "status": "unobserved_feed_failed",
                              "case_entry_ids": [], "skipped_by_adapter": False, "error": str(last_err)}
                    out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out_f.flush()
                    outcomes.append(record)
                    continue

                case_ids = _wait_terminal(memory_root, sid, log_path, start_offset)
                end_offset = os.path.getsize(log_path) if os.path.exists(log_path) else start_offset
                log_window = read_log_window(log_path, start_offset, end_offset) if os.path.exists(log_path) else ""

                if result.get("skipped"):
                    status = "unobserved_excluded"
                else:
                    status = classify_session(log_window, case_ids)

                record = {"external_id": eid, "stratum": stratum, "status": status,
                          "case_entry_ids": case_ids, "skipped_by_adapter": bool(result.get("skipped"))}
                out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_f.flush()
                outcomes.append(record)

    print(f"done: {len(outcomes)} sessions recorded to {outcomes_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--memory-root", required=True)
    ap.add_argument("--log-path", required=True)
    ap.add_argument("--outcomes", required=True)
    ap.add_argument("--admin-base", default=os.environ.get("LITELLM_ADMIN_BASE", ""))
    ap.add_argument("--admin-key", default=os.environ.get("LITELLM_ADMIN_KEY", ""))
    ap.add_argument("--target-key", default=os.environ.get("EVEROS_M1B_KEY", ""))
    ap.add_argument("--cap-usd", type=float, default=10.0)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    run(a.snapshot, a.base_url, a.memory_root, a.log_path, a.outcomes,
        a.admin_base, a.admin_key, a.target_key, a.cap_usd, limit=a.limit)


if __name__ == "__main__":
    main()
