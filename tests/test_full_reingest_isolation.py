"""full-reingest.sh T13 接线契约测试（PR4 伴随子 PR：reingest 隔离与可核验）。

stub 面：curl（健康检查放行）、cass-stub（覆盖 --version/status/stats/sources/index 全部子命令，
用一份固定 status JSON 满足所有 serving 完整性判据）、recall-stub.py（召回门 no-op 成功，记录 argv
以核验 RECALL_ARGS 透传）。真跑 bash 脚本到收尾，不 mock 脚本自身逻辑——只锁接线契约，不锁叙事。
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "infra" / "cass-semantic" / "full-reingest.sh"

STATUS_JSON = json.dumps(
    {
        "index": {"exists": True},
        "db_vector_domain": {
            "active": True,
            "audit_status": "passed",
            "error": None,
            "embedded_count": 1005,
        },
        "database": {"opened": True},
    }
)

CASS_STUB_BODY = r"""
case "$1" in
  --version)
    echo "cass 0.6.17"
    ;;
  status)
    # 真 wrapper（cass-cand.sh）每次调用都往 stderr 打一行诊断 JSON——这里复刻该行为，
    # 回归锁住"serving 完整性那次 status --json 调用必须 stdout/stderr 分流"（T13 既有缺陷经
    # wrapper 首次暴露：曾经 2>&1 合流导致两段 JSON 拼接、json.load 报 Extra data）。
    echo '{"db_path":"stub","home":"stub-home","binary_sha256":"stub-sha"}' >&2
    cat "$STUB_STATUS_JSON"
    ;;
  stats)
    printf '{"conversations": %s}\n' "${STUB_CONV_COUNT:-42}"
    ;;
  sources)
    echo '{}'
    ;;
  index)
    case "$2" in
      --full)
        printf '{"total_conversations": %s}\n' "${STUB_CONV_COUNT:-42}"
        ;;
      --force-rebuild)
        echo '{}'
        ;;
      --semantic)
        echo '{"success": true, "activated": true}'
        ;;
      *)
        echo '{}'
        ;;
    esac
    ;;
  *)
    echo '{}'
    ;;
esac
exit 0
""".strip()

RECALL_STUB_BODY = """
import os
import sys

log = os.environ.get("RECALL_CALL_LOG")
if log:
    with open(log, "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(0)
"""


def _stub(dir_: pathlib.Path, name: str, body: str) -> pathlib.Path:
    p = dir_ / name
    p.write_text(f"#!/usr/bin/env bash\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _base_env(tmp_path: pathlib.Path, *, log_root: pathlib.Path) -> dict[str, str]:
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(exist_ok=True)
    status_json_path = tmp_path / "status.json"
    status_json_path.write_text(STATUS_JSON)

    _stub(stub_bin, "curl", "exit 0")
    cass_stub = _stub(stub_bin, "cass-stub", CASS_STUB_BODY)

    recall_stub = tmp_path / "recall-stub.py"
    recall_stub.write_text(RECALL_STUB_BODY)

    home = tmp_path / "home"
    (home / ".local" / "share").mkdir(parents=True, exist_ok=True)

    env = {
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "HOME": str(home),
        "CANON_DATA_DIR": str(tmp_path / "canon"),
        "NEW_DATA_DIR": str(tmp_path / "canon.new"),
        "CASS_BIN": str(cass_stub),
        "CASS_INFINITY_URL": "http://127.0.0.1:7997",
        "RECALL_RUN": str(recall_stub),
        "CASS_WRITE_LOCK": str(tmp_path / "write.lock"),
        "REINGEST_LOG_ROOT": str(log_root),
        "STUB_STATUS_JSON": str(status_json_path),
    }
    return env


def test_dry_run_emits_json_stamp_pass_line_and_ready_sentinel(tmp_path: pathlib.Path) -> None:
    log_root = tmp_path / "logs"
    env = _base_env(tmp_path, log_root=log_root)

    # ingest_pass 的 kill -0 轮询固定 sleep 60（脚本既有设计，T13 未改），故每条全流程用例至少要给
    # 一次轮询周期的余量；90s 覆盖单遍 live ingest 的最坏情况。
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=90, env=env)
    assert r.returncode == 0, f"干跑（无 SWAP）应成功收尾:\n{r.stdout}\n{r.stderr}"

    assert "PASS live conversations=42" in r.stdout
    assert "GATE PASSED." in r.stdout
    assert "READY TO SWAP run_id=" in r.stdout
    assert "SWAPPED run_id=" not in r.stdout, "无 SWAP=1 不应打印 SWAPPED 哨兵"

    run_json_path = log_root / "reingest-run.json"
    assert run_json_path.is_file(), "reingest-run.json 必须落在 REINGEST_LOG_ROOT 下"
    stamp = json.loads(run_json_path.read_text().strip().splitlines()[0])
    for key in ("run_id", "bin", "exec_bin", "bin_sha256", "canon", "new", "lock", "log_root", "mirror_home"):
        assert key in stamp, f"JSON 印记缺字段 {key}"
    assert stamp["bin"] == env["CASS_BIN"]
    assert stamp["exec_bin"] == env["CASS_BIN"], "未设 CASS_CAND_BIN 时 exec_bin 应回落到 $BIN"
    assert stamp["mirror_home"] == "", "未设 MIRROR_HOME 时应为空串"
    assert stamp["log_root"] == str(log_root)
    assert pathlib.Path(stamp["canon"]).name == "canon"
    assert pathlib.Path(stamp["new"]).name == "canon.new"

    # stdout 首行也应是同一份 JSON（tee 到 stdout 与文件）
    first_line = r.stdout.strip().splitlines()[0]
    assert json.loads(first_line) == stamp

    # 回归锁：serving 完整性那次 status --json 必须 stdout/stderr 分流，不能被 wrapper 的诊断行
    # 污染成两段 JSON 拼接（曾经 `2>&1` 合流触发 json.load Extra data，让健康库被误判 FAIL）。
    status_json_path = log_root / "cc-reingest-status.json"
    status_stderr_path = log_root / "cc-reingest-status.stderr"
    parsed_status = json.loads(status_json_path.read_text())  # 必须整份可解析，不含多余数据
    assert parsed_status["index"]["exists"] is True
    assert status_stderr_path.is_file(), "wrapper 的诊断行必须单独落盘，不是被吞掉"
    assert "db_path" in status_stderr_path.read_text()

    # 隔离核验：所有 cc-reingest-* 产物落在自定义 log_root 下，不外溢
    reingest_files = sorted(p.name for p in log_root.glob("cc-reingest-*"))
    assert reingest_files, "log_root 下应有 cc-reingest-* 产物"


def test_bin_sha256_hashes_exec_bin_not_wrapper(tmp_path: pathlib.Path) -> None:
    """接口④「哈希对象 = 真执行二进制」：设置 CASS_CAND_BIN 后，bin_sha256 必须对它取哈希，
    而不是对 $BIN（wrapper）取哈希；exec_bin 字段同样回落到该真执行二进制路径。"""
    log_root = tmp_path / "logs"
    env = _base_env(tmp_path, log_root=log_root)

    cand_bin = tmp_path / "cand-bin"
    cand_bin.write_text("not-a-real-binary-just-needs-distinct-bytes\n")
    cand_bin.chmod(cand_bin.stat().st_mode | stat.S_IEXEC)
    env["CASS_CAND_BIN"] = str(cand_bin)

    # ingest_pass 的 kill -0 轮询固定 sleep 60（脚本既有设计，T13 未改），故每条全流程用例至少要给
    # 一次轮询周期的余量；90s 覆盖单遍 live ingest 的最坏情况。
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=90, env=env)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"

    stamp = json.loads((log_root / "reingest-run.json").read_text().strip().splitlines()[0])
    assert stamp["exec_bin"] == str(cand_bin)
    assert stamp["bin"] == env["CASS_BIN"]
    assert stamp["bin"] != stamp["exec_bin"]

    import hashlib

    expected = hashlib.sha256(cand_bin.read_bytes()).hexdigest()
    assert stamp["bin_sha256"] == expected, "bin_sha256 必须是 exec_bin(=CASS_CAND_BIN) 的哈希，不是 wrapper 的"


def test_recall_args_passthrough(tmp_path: pathlib.Path) -> None:
    log_root = tmp_path / "logs"
    env = _base_env(tmp_path, log_root=log_root)
    call_log = tmp_path / "recall-calls.log"
    env["RECALL_CALL_LOG"] = str(call_log)
    env["RECALL_ARGS"] = "--foo bar --baz"

    # ingest_pass 的 kill -0 轮询固定 sleep 60（脚本既有设计，T13 未改），故每条全流程用例至少要给
    # 一次轮询周期的余量；90s 覆盖单遍 live ingest 的最坏情况。
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=90, env=env)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"

    recorded = call_log.read_text().strip().splitlines()
    assert len(recorded) == 1
    argv = recorded[0].split(" ")
    assert argv[0] == env["CASS_BIN"]
    assert argv[1:] == ["--foo", "bar", "--baz"], f"RECALL_ARGS 未按词展开透传：{argv!r}"


def test_swap1_emits_swapped_sentinel(tmp_path: pathlib.Path) -> None:
    log_root = tmp_path / "logs"
    env = _base_env(tmp_path, log_root=log_root)
    env["SWAP"] = "1"

    # ingest_pass 的 kill -0 轮询固定 sleep 60（脚本既有设计，T13 未改），故每条全流程用例至少要给
    # 一次轮询周期的余量；90s 覆盖单遍 live ingest 的最坏情况。
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=90, env=env)
    assert r.returncode == 0, f"SWAP=1 干跑应成功收尾:\n{r.stdout}\n{r.stderr}"
    assert "SWAPPED run_id=" in r.stdout
    assert "READY TO SWAP run_id=" not in r.stdout, "SWAP=1 不应打印 READY TO SWAP 哨兵"

    canon = pathlib.Path(env["CANON_DATA_DIR"])
    assert canon.is_dir(), "swap 后 canon 目录应存在（由 NEW mv 而来）"


def test_lock_contention_exits_75_not_0(tmp_path: pathlib.Path) -> None:
    """接口③：锁竞争是可重试的临时失败，必须 exit 75（EX_TEMPFAIL），不再假装成功退出 0。
    锁测试不需要真跑摄入——先占锁，脚本应在 flock 失败处立即退出。"""
    log_root = tmp_path / "logs"
    env = _base_env(tmp_path, log_root=log_root)
    lock_path = pathlib.Path(env["CASS_WRITE_LOCK"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    import fcntl

    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=30, env=env)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert r.returncode == 75, f"锁竞争应 exit 75，实得 {r.returncode}:\n{r.stdout}\n{r.stderr}"
    assert "another cass write holds lock" in r.stdout
    # 锁竞争路径不应留下 reingest-run.json（在 flock 失败处已退出，晚于它的开场留痕不执行）
    assert not (log_root / "reingest-run.json").exists()
