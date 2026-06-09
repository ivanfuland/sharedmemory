"""M0 范围两条独立腿（非串联，完整 capture→桥→写串联是 M3）：
- read_leg：按 canonical 契约 JOIN read_sql 读出真实消息，必需字段非空 → 读端可执行
- write_leg：gbrain timeline-add reconcile 幂等（原生去重 + 重跑零增） → 写端幂等"""
import json
import os
import sqlite3
import subprocess
import pathlib
import uuid
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
GBRAIN_HOME = os.environ["GBRAIN_HOME"]
CANON_DB = os.environ.get("CASS_CANON_DB")
CANON_FIELDS = REPO / "contracts" / "cass-canonical-fields.json"
GBRAIN_FIELDS = REPO / "contracts" / "gbrain-io-fields.json"
CANON_ACTIVE = bool(CANON_DB and pathlib.Path(CANON_DB or "").exists() and CANON_FIELDS.exists())


def test_exactly_one_read_path_active():
    """守门：读端路线恰好 canonical 成立（本机实测 canonical；无 fallback artifact）。"""
    fallback = (REPO / "fixtures" / "jsonl-field-paths.json").exists()
    assert CANON_ACTIVE ^ fallback, f"读端路线须恰好一条（canonical={CANON_ACTIVE} fallback={fallback}）"


@pytest.mark.skipif(not CANON_ACTIVE, reason="canonical 路线未启用")
def test_read_leg_canonical_join_executable():
    """读腿：JOIN read_sql 读一条真实消息，必需字段全非空。"""
    f = json.loads(CANON_FIELDS.read_text())
    con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True)
    try:
        cur = con.execute(f"{f['read_sql']} WHERE m.id > -1 ORDER BY cursor ASC LIMIT 1")
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
    finally:
        con.close()
    assert row is not None, "canonical 应至少一条消息"
    rec = dict(zip(cols, row))
    for alias in f["required_aliases"]:
        assert rec.get(alias) not in (None, ""), f"读出行必需字段 {alias} 为空"


@pytest.mark.needs_gbrain
def test_write_leg_reconcile_idempotent():
    """写腿：timeline-add 同条目两次（模拟崩溃重跑）→ timeline 只 1 条（原生去重）。"""
    env = {**os.environ, "GBRAIN_HOME": GBRAIN_HOME,
           "PATH": os.path.expanduser("~/.bun/bin") + ":" + os.environ.get("PATH", "")}
    def g(*a, stdin=None):
        r = subprocess.run(["gbrain", *a], input=stdin, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"gbrain {a}: {r.stderr or r.stdout}")
        return r.stdout
    slug = f"people/leg-{uuid.uuid4().hex[:8]}"
    key = uuid.uuid4().hex[:12]
    g("put", slug, stdin="# leg\n\nbody\n")
    for _ in range(2):  # 重跑
        g("timeline-add", slug, "2026-06-09", f"幂等腿 [dk:{key}]")
    n = sum(1 for l in g("timeline", slug).splitlines() if key in l)
    assert n == 1, f"reconcile 后同 key 应 1 条，实际 {n}"
