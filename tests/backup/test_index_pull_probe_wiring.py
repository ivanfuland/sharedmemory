"""index-pull.sh × 结构探针接线集成测试(codex R2-F4):真跑脚本到探针段,
断言 exit 3 + stdout 末行 JSON 契约(runner.ts 只消费这个)。

stub 面:curl(健康检查放行)与 CASS_BIN(词法段 no-op 成功);sqlite3/timeout/find 全真。
"""
from __future__ import annotations

import json
import os
import pathlib
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


def test_healthy_db_passes_probe_and_reaches_semantic_phase(tmp_path: pathlib.Path) -> None:
    """健康库探针放行——脚本走过 1b 段(之后语义段行为不在本测试范围,只需非探针路径退出)。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _mk_multipage_db(tmp_path).rename(data_dir / "agent_search.db")

    home = tmp_path / "home"
    (home / ".local" / "share").mkdir(parents=True)
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    _stub(stub_bin, "curl", "exit 0")
    _stub(stub_bin, "cass-stub", "exit 0")

    env = {
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "HOME": str(home),
        "CASS_DATA_DIR": str(data_dir),
        "CASS_BIN": str(stub_bin / "cass-stub"),
        "CASS_PULL_LOG_DIR": str(tmp_path / "logs"),
    }
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode != 3, f"健康库不得走探针失败路径:\n{r.stdout}\n{r.stderr}"
    assert "STRUCTURE_PROBE_FAIL" not in r.stdout