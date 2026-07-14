import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_scan_maps_sids_to_entry_ids(tmp_path):
    d = tmp_path / "agents" / "a" / ".cases"
    d.mkdir(parents=True)
    (d / "agent_case-2026-07-14.md").write_text(
        "<!-- entry:ac_1 -->\n**session_id**: prod-aa\n<!-- /entry:ac_1 -->\n"
        "<!-- entry:ac_2 -->\n**session_id**: prod-bb\n<!-- /entry:ac_2 -->\n",
        encoding="utf-8")
    sids = tmp_path / "sids.json"
    sids.write_text(json.dumps(["prod-aa", "prod-zz"]), encoding="utf-8")
    p = subprocess.run(
        [sys.executable, "-m", "scripts.everos_scan_sessions",
         f"--memory-root={tmp_path}", f"--sids-file={sids}"],
        cwd=REPO, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.strip())
    assert out == {"prod-aa": ["ac_1"]}   # prod-bb 不在候选、prod-zz 没出卡


def test_scan_missing_root_returns_empty(tmp_path):
    sids = tmp_path / "sids.json"
    sids.write_text(json.dumps(["prod-aa"]), encoding="utf-8")
    p = subprocess.run(
        [sys.executable, "-m", "scripts.everos_scan_sessions",
         f"--memory-root={tmp_path / 'nope'}", f"--sids-file={sids}"],
        cwd=REPO, capture_output=True, text=True, timeout=60)
    assert json.loads(p.stdout.strip()) == {}
