#!/usr/bin/env python3
"""M1c Phase 1 评估编排(操作性,不进 pytest)。子命令:
  build-queryset --db <cass.db> --snapshot <snapshot.json> --out <dir>
  retrieve       --queryset <queryset.jsonl> --base http://127.0.0.1:8010 --out <dir>
  make-jobs      --stage {l1,top5,foresight} --workdir <dir> --instance <pro-instance 副本>
  assemble       --workdir <dir>     # verdicts -> QueryOutcome -> metrics.json
台账全落 --out/--workdir(默认 ~/everos-m1b-data/m1c-eval/),不进 git。
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from everos_eval.corpus import load_all_cards, load_entries
from everos_eval.judge_io import build_foresight_jobs, build_l1_jobs, build_top5_jobs, parse_verdicts
from everos_eval.queryset import (first_user_messages, load_snapshot_eids, raw_baseline,
                                  scan_complex_candidates, select_candidates)
from everos_eval.retrieve import canonical_id, merge_top5, search
from everos_eval.stats import QueryOutcome, band_verdict, compute_metrics
from everos_probe.sampling import fetch_rows  # ro 行读取复用
from datetime import datetime, timedelta, timezone

# M1b 快照截止(Asia/Shanghai)对应的毫秒 epoch;CASS created_at 是毫秒整数
CUTOFF_MS = int(datetime(2026, 7, 13, 23, 59, 59,
                         tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
# 评估开工日排除线(spec R6):当天起的会话是评估自身产生的(subagent/审查),入选即自指污染
EVAL_DAY_MS = int(datetime(2026, 7, 14, 0, 0, 0,
                           tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)


def _append(path: Path, rec: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


def cmd_build_queryset(a):
    import sqlite3
    cands = scan_complex_candidates(Path(a.db))
    n_all = len(cands)
    cands = [c for c in cands if c.first_ts_ms < EVAL_DAY_MS]  # spec R6 排除线
    print(f"R6 排除评估开工日后的会话: {n_all} -> {len(cands)}")
    eids = load_snapshot_eids(Path(a.snapshot))
    chosen, tier = select_candidates(cands, eids, CUTOFF_MS, target=30)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    qp = out / "queryset.jsonl"
    if qp.exists():
        sys.exit(f"{qp} 已存在(冻结后不得重建);要重跑先手动 trash")
    with sqlite3.connect(f"file:{a.db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row  # fetch_rows 用 dict(r),缺这行必崩(codex R1)
        for i, c in enumerate(chosen, 1):
            users = first_user_messages(fetch_rows(conn, c.conversation_id))
            _append(qp, {"query_id": f"q{i:02d}", "external_id": c.external_id,
                         "source": c.source, "n_rounds": c.n_rounds, "tier": tier,
                         "first_user_messages": users, "raw_baseline": raw_baseline(users),
                         "query": None})  # query 由主会话 subagent 生成后回填(冻结 prompt 另存)
    print(f"queryset frozen: {qp} tier={tier} n={len(chosen)}")


def cmd_retrieve(a):
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    rp = out / "retrieval.jsonl"
    for line in Path(a.queryset).read_text(encoding="utf-8").splitlines():
        q = json.loads(line)
        assert q["query"], f"{q['query_id']} 缺生成查询,先回填再检索"
        for variant, qtext in (("synthetic", q["query"]), ("raw", q["raw_baseline"])):
            resp = search(a.base, "everos-m1b-probe", qtext)
            data = resp["data"]
            top5 = merge_top5(data.get("agent_cases", []), data.get("agent_skills", []))
            for it in top5:  # 归一为 canonical entry id(与 L1 gold 同一 id 空间;raw 留在 payload)
                it["id"] = canonical_id(it["id"], it["mem_type"])
            _append(rp, {"query_id": q["query_id"], "variant": variant,
                         "top5": top5, "raw_response": data})
    print(f"retrieval ledger: {rp}")


def cmd_make_jobs(a):
    wd = Path(a.workdir)
    inst = Path(a.instance)
    cards = load_all_cards(inst)
    if a.stage == "l1":
        queries = [json.loads(x) for x in (wd / "queryset.jsonl").read_text(encoding="utf-8").splitlines()]
        jobs = build_l1_jobs(queries, cards)
    elif a.stage == "top5":
        text_by_id = {c.entry_id: c.text for c in cards}
        jobs = []
        for line in (wd / "retrieval.jsonl").read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["variant"] != "synthetic":
                continue  # 主门只判 synthetic;raw 对照仅复用 l1/top5 有用性判定口径,见 assemble
            q = {"query_id": r["query_id"], "query": _query_text(wd, r["query_id"])}
            jobs.extend(build_top5_jobs(q, r["top5"], text_by_id))
    else:
        fs_file = next((inst / "default_app/default_project/users/demo-user/.foresights").glob("foresight-*.md"))
        import hashlib
        entries = sorted(load_entries(fs_file, "fs", "foresight"),
                         key=lambda c: hashlib.sha256(c.entry_id.encode()).hexdigest())[:60]
        jobs = build_foresight_jobs(entries)
    jp = wd / f"{a.stage}_jobs.jsonl"
    jp.write_text("\n".join(json.dumps(j, ensure_ascii=False) for j in jobs), encoding="utf-8")
    print(f"{len(jobs)} jobs -> {jp}")


def _query_text(wd: Path, qid: str) -> str:
    for line in (wd / "queryset.jsonl").read_text(encoding="utf-8").splitlines():
        q = json.loads(line)
        if q["query_id"] == qid:
            return q["query"]
    raise KeyError(qid)


def cmd_assemble(a):
    wd = Path(a.workdir)
    def _expected(stage):
        return {json.loads(x)["job_id"]
                for x in (wd / f"{stage}_jobs.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()}
    l1, l1_failed = parse_verdicts(wd / "l1_verdicts.jsonl", "l1", expected_job_ids=_expected("l1"))
    t5, t5_failed = parse_verdicts(wd / "top5_verdicts.jsonl", "top5", expected_job_ids=_expected("top5"))
    if l1_failed or t5_failed:
        sys.exit(f"verdict 有坏行,先重判再 assemble: l1={l1_failed[:5]} top5={t5_failed[:5]}")
    retr = {}
    for line in (wd / "retrieval.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        retr.setdefault(r["query_id"], {})[r["variant"]] = r["top5"]
    outcomes = []
    for line in (wd / "queryset.jsonl").read_text(encoding="utf-8").splitlines():
        q = json.loads(line); qid = q["query_id"]
        rel = frozenset(v["card_id"] for k, v in _by_q(l1, "l1", qid) if v["relevant"])
        use = frozenset(v["card_id"] for k, v in _by_q(l1, "l1", qid) if v["useful"])
        top5 = tuple(t["id"] for t in retr[qid]["synthetic"])
        t5rel = frozenset(v["card_id"] for k, v in _by_q(t5, "top5", qid) if v["relevant"])
        t5use = frozenset(v["card_id"] for k, v in _by_q(t5, "top5", qid) if v["useful"])
        outcomes.append(QueryOutcome(qid, rel, use, top5, t5rel, t5use))
    m = compute_metrics(outcomes)
    m["main_gate_verdict"] = band_verdict(m["covered_useful_hit_at_5"], m["n_covered"],
                                          m["covered_useful_hit_wilson_lo"])
    # generator sensitivity(spec §10 15pp):raw 与 synthetic 同口径同裁判源 = L1 useful 集合
    l1_useful = {}
    for line in (wd / "queryset.jsonl").read_text(encoding="utf-8").splitlines():
        q = json.loads(line)
        l1_useful[q["query_id"]] = frozenset(
            v["card_id"] for k, v in _by_q(l1, "l1", q["query_id"]) if v["useful"])

    def _l1_based_hit(variant: str) -> float:
        n = len(l1_useful)
        return sum(1 for qid, use in l1_useful.items()
                   if {t["id"] for t in retr[qid][variant]} & use) / n if n else 0.0

    m["synthetic_useful_hit_l1based"] = _l1_based_hit("synthetic")
    m["raw_useful_hit_l1based"] = _l1_based_hit("raw")
    m["generator_sensitivity_delta"] = abs(m["synthetic_useful_hit_l1based"] - m["raw_useful_hit_l1based"])
    m["generator_sensitivity_flag"] = m["generator_sensitivity_delta"] > 0.15  # 预注册:>15pp 报告专节
    # foresight 语料抽审汇总(spec §4 必需诊断项,缺失即 fail,不许静默缺——codex R2)
    fs_path = wd / "foresight_verdicts.jsonl"
    if not fs_path.exists():
        sys.exit("缺 foresight_verdicts.jsonl(spec 必需诊断项),先跑 foresight 判定再 assemble")
    fs, fs_failed = parse_verdicts(fs_path, "foresight", expected_job_ids=_expected("foresight"))
    if fs_failed:
        sys.exit(f"foresight verdict 有坏行: {fs_failed[:5]}")
    from collections import Counter
    cats = Counter(v["category"] for v in fs.values())
    m["foresight_categories"] = dict(cats)
    m["foresight_noise_ratio"] = (cats.get("instruction_echo", 0) + cats.get("trivial", 0)) / max(len(fs), 1)
    (wd / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(m, ensure_ascii=False, indent=2))


def _by_q(verdicts: dict, kind: str, qid: str):
    # job_id 格式: l1 = "l1:{qid}:{card_id}"(maxsplit=2), top5 = "top5:{qid}:{rank}:{card_id}"(maxsplit=3)。
    # card_id 是自由文本可能含半角冒号,不能用无限制 split(":") 切,否则静默切碎错配(codex review)。
    maxsplit = {"l1": 2, "top5": 3}[kind]
    for k, v in verdicts.items():
        parts = k.split(":", maxsplit)
        if parts[0] == kind and parts[1] == qid:
            v = dict(v); v["card_id"] = parts[-1]
            yield k, v


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-queryset"); b.add_argument("--db", required=True); b.add_argument("--snapshot", required=True); b.add_argument("--out", required=True); b.set_defaults(fn=cmd_build_queryset)
    r = sub.add_parser("retrieve"); r.add_argument("--queryset", required=True); r.add_argument("--base", required=True); r.add_argument("--out", required=True); r.set_defaults(fn=cmd_retrieve)
    j = sub.add_parser("make-jobs"); j.add_argument("--stage", choices=["l1", "top5", "foresight"], required=True); j.add_argument("--workdir", required=True); j.add_argument("--instance", required=True); j.set_defaults(fn=cmd_make_jobs)
    s = sub.add_parser("assemble"); s.add_argument("--workdir", required=True); s.set_defaults(fn=cmd_assemble)
    a = ap.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
