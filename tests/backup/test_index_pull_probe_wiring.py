"""index-pull.sh × 结构探针接线集成测试(codex R2-F4):真跑脚本到探针段,
断言 exit 3 + stdout 末行 JSON 契约(runner.ts 只消费这个)。

stub 面:curl(健康检查放行)与 CASS_BIN(词法段 no-op 成功);sqlite3/timeout/find 全真。
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import stat
import subprocess

from test_structure_probe import _corrupt_root_separator, _mk_multipage_db

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "infra" / "cass-semantic" / "index-pull.sh"


def _stub(dir_: pathlib.Path, name: str, body: str) -> None:
    p = dir_ / name
    p.write_text(f"#!/usr/bin/env bash\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def test_probe_failure_emits_exit3_json(tmp_path: pathlib.Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = _mk_multipage_db(tmp_path)
    _corrupt_root_separator(db)
    db.rename(data_dir / "agent_search.db")

    home = tmp_path / "home"
    (home / ".local" / "share").mkdir(parents=True)
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    _stub(stub_bin, "curl", "exit 0")          # Infinity 健康检查放行
    _stub(stub_bin, "cass-stub", "exit 0")     # 词法 index no-op 成功 → 走到探针

    env = {
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "HOME": str(home),
        "CASS_DATA_DIR": str(data_dir),
        "CASS_BIN": str(stub_bin / "cass-stub"),
        "CASS_PULL_LOG_DIR": str(tmp_path / "logs"),
    }
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=env)

    assert r.returncode == 3, f"探针红应 exit 3:rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    last = r.stdout.strip().splitlines()[-1]
    payload = json.loads(last)   # runner 契约:末行必须是合法 JSON
    assert payload["ok"] is False
    assert "STRUCTURE_PROBE_FAIL" in payload["error"]
    assert "seek-invisible" in payload["error"]


def test_healthy_db_passes_probe_and_rotation_is_nul_safe(tmp_path: pathlib.Path) -> None:
    """健康库探针放行 + 轮转在**含空格目录**下 NUL 安全(codex R3-P1,跑真脚本真轮转):
    预置 60 份旧日志,脚本跑完后 = 48 份(47 旧 + 本次),零拆词误删。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _mk_multipage_db(tmp_path).rename(data_dir / "agent_search.db")

    home = tmp_path / "home"
    (home / ".local" / "share").mkdir(parents=True)
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    _stub(stub_bin, "curl", "exit 0")
    _stub(stub_bin, "cass-stub", "exit 0")

    log_dir = tmp_path / "log dir with spaces"
    log_dir.mkdir()
    for i in range(60):
        f = log_dir / f"run-2026071500{i:02d}00-9.log"
        f.write_text("old")
        os.utime(f, (1_000_000_000 + i, 1_000_000_000 + i))

    env = {
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "HOME": str(home),
        "CASS_DATA_DIR": str(data_dir),
        "CASS_BIN": str(stub_bin / "cass-stub"),
        "CASS_PULL_LOG_DIR": str(log_dir),
    }
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode != 3, f"健康库不得走探针失败路径:\n{r.stdout}\n{r.stderr}"
    assert "STRUCTURE_PROBE_FAIL" not in r.stdout
    kept = sorted(log_dir.glob("run-*.log"))
    assert len(kept) == 48, f"轮转应留 48 份(47 旧+本次),实得 {len(kept)}"


def test_preflight_bypass_is_scoped_to_lexical_index(tmp_path: pathlib.Path) -> None:
    """即使父环境带 hostile 值，也只允许词法 index 收到 preflight bypass。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = data_dir / "agent_search.db"
    _mk_multipage_db(tmp_path).rename(db)

    sentinel = tmp_path / "sentinel.jsonl"
    sentinel.write_text("{}\n")
    with sqlite3.connect(db) as con:
        con.execute("ALTER TABLE conversations ADD COLUMN source_path TEXT")
        con.execute(
            "UPDATE conversations SET source_path = ? WHERE id = 2000",
            (str(sentinel),),
        )

    vector_dir = data_dir / "vector_index"
    vector_dir.mkdir()
    manifest = vector_dir / "semantic_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "quality_tier": {
                    "ready": False,
                    "db_fingerprint": "content-v1:stale",
                }
            }
        )
    )
    home = tmp_path / "home"
    (home / ".local" / "share").mkdir(parents=True)
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    calls = tmp_path / "cass-calls.tsv"
    _stub(stub_bin, "curl", "exit 0")
    _stub(
        stub_bin,
        "cass-stub",
        r"""
printf '%s\t%s\n' "${CASS_SKIP_PREFLIGHT_COUNT_TOTAL_MESSAGES-unset}" "$*" >> "$CASS_STUB_CALLS"
if [ "${1:-}" = "models" ] && [ "${2:-}" = "backfill" ]; then
  printf '{"published":true,"last_offset":0}\n'
fi
exit 0
""".strip(),
    )

    env = {
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "HOME": str(home),
        "CASS_DATA_DIR": str(data_dir),
        "CASS_BIN": str(stub_bin / "cass-stub"),
        "CASS_PULL_LOG_DIR": str(tmp_path / "logs"),
        "CASS_SKIP_PREFLIGHT_COUNT_TOTAL_MESSAGES": "hostile-parent",
        "CASS_STUB_CALLS": str(calls),
    }
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    assert r.returncode == 0, f"wrapper 应完整成功:\n{r.stdout}\n{r.stderr}"
    assert json.loads(r.stdout.strip().splitlines()[-1])["ok"] is True
    observed = [line.split("\t", 1) for line in calls.read_text().splitlines()]
    assert observed[0] == ["1", "index"]
    assert observed[1][0] == "unset"
    assert observed[1][1].startswith("index --semantic --embedder infinity --watch-once ")
    assert observed[2][0] == "unset"
    assert observed[2][1].startswith("models backfill --tier quality ")
    assert len(observed) == 3
