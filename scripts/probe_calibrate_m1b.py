"""M1b probe 仪器对照（Phase B Task 6，spec §3 probe_calibrate / §8 R2 / §11 验收第一条）。

喂 3 个已知结局的合成会话，确认 scan_terminal + EverOS 日志归因把三者分别正确判为
过 / 结构拒 / 语义拒；同时把本地 count_tool_rounds 的口径与 EverOS 日志 `only N rounds`
对照（不一致则改本地口径——见 everos_probe/sampling.py 的 count_tool_rounds 文档字符串）。
仪器分类错 -> 先修仪器，不信真样本的数。操作性脚本，喂的是真 EverOS 实例，不进 pytest。

用法：probe_calibrate_m1b.py BASE_URL MEMORY_ROOT LOG_PATH [RUN_TAG]
RUN_TAG 可选，用于重跑时区分 session_id（避免同实例 session 撞键）。
"""
from __future__ import annotations

import os
import sys
import time

import msgpack

from everos_adapter.cap import make_clamper
from everos_adapter.pipeline import run_session
from everos_adapter.scan_terminal import find_session_case_files, session_case_entry_ids
from everos_probe.attribution import classify_session, read_log_window
from everos_probe.sampling import count_tool_rounds

AGENT_ID = "everos-m1b-calibrate"
USER_SENDER = "demo-user"


def _blob(d):
    return msgpack.packb(d, use_bin_type=True)


def _call(idx, tcid, ts, content='Bash({"command":"ls"})'):
    return {"idx": idx, "role": "tool_call", "content": content, "created_at": ts,
            "extra_bin": _blob({"tool_call_id": tcid, "tool_call_args": {"command": "ls"}}),
            "extra_json": None}


def _result(idx, tcid, ts, content="ok"):
    return {"idx": idx, "role": "tool_result", "content": content, "created_at": ts,
            "extra_bin": _blob({"tool_call_id": tcid}), "extra_json": None}


def _msg(idx, role, content, ts):
    return {"idx": idx, "role": role, "content": content, "created_at": ts,
            "extra_bin": None, "extra_json": None}


def known_pass_session() -> list:
    """7 轮 tool-call、含两次失败回退 + 用户纠正方向 + 非显而易见根因（模块级共享队列
    竞态）的 hard-won discovery：先怀疑缓冲区、改了还失败、被用户纠正方向、深挖发现隐藏
    的并发竞态才修好——有明确 detours / errors / user correction / non-obvious root cause，
    预期过语义门（这是仪器要锚定的『真 pass』一格）。"""
    rows = [_msg(0, "user", "test_worker 偶发失败，大概三次跑一次挂，帮我查", 1000)]
    rows += [_call(1, "c0", 1010, 'exec_command({"cmd":"pytest tests/test_worker.py -x"})'),
             _result(2, "c0", 1011, "PASSED (1 passed)")]
    rows += [_call(3, "c1", 1020, 'exec_command({"cmd":"pytest tests/test_worker.py -x --count=10"})'),
             _result(4, "c1", 1021, "FAILED test_worker.py::test_flush - AssertionError: expected 5 items, got 3 (run 4/10)")]
    rows += [_msg(5, "assistant", "偶发，像是缓冲区没刷完就断言了，我加个 flush 试试", 1025)]
    rows += [_call(6, "c2", 1030, 'exec_command({"cmd":"add explicit buffer.flush() before assert"})'),
             _result(7, "c2", 1031, "edited tests/test_worker.py")]
    rows += [_call(8, "c3", 1040, 'exec_command({"cmd":"pytest tests/test_worker.py --count=10"})'),
             _result(9, "c3", 1041, "FAILED (run 7/10) - same AssertionError, flush 没用")]
    rows += [_msg(10, "user", "不是刷新的问题，你看看是不是两个 worker 共享了那个队列", 1050)]
    rows += [_call(11, "c4", 1060, 'exec_command({"cmd":"grep -n shared_queue src/worker.py"})'),
             _result(12, "c4", 1061, "src/worker.py:12: shared_queue = Queue()  # module-level, shared across workers")]
    rows += [_msg(13, "assistant", "找到了：shared_queue 是模块级单例，并发两个 worker 时互相偷对方的 item，不是缓冲是竞态", 1070)]
    rows += [_call(14, "c5", 1080, 'exec_command({"cmd":"make shared_queue a per-worker instance"})'),
             _result(15, "c5", 1081, "edited src/worker.py")]
    rows += [_call(16, "c6", 1090, 'exec_command({"cmd":"pytest tests/test_worker.py --count=20"})'),
             _result(17, "c6", 1091, "20 passed")]
    rows.append(_msg(18, "assistant", "根因是模块级 shared_queue 被多 worker 共享导致偷单，改成每 worker 独立队列后跑 20 次全过", 1100))
    return rows


def known_structural_reject_session() -> list:
    """1 轮 tool-call，< min_tool_call_rounds=3，Step 3b 结构门必拒（与 LLM 判断无关，
    确定性最强的一格）。"""
    rows = [_msg(0, "user", "看下这个文件有多少行", 1000)]
    rows += [_call(1, "c0", 1010, 'exec_command({"cmd":"wc -l src/foo.py"})'),
             _result(2, "c0", 1011, "42 src/foo.py")]
    rows.append(_msg(3, "assistant", "42 行", 1020))
    return rows


def known_semantic_reject_session() -> list:
    """4 轮、纯顺风顺水直线流程（读文件->改->测->过，零报错零回退），预期被语义门拒
    （EverOS M0 RUNBOOK 实测原话："Straightforward march: run test, read source, edit,
    verify — no detours or surprises."）。"""
    rows = [_msg(0, "user", "给 foo() 加个 docstring", 1000)]
    rows += [_call(1, "c0", 1010, 'exec_command({"cmd":"cat src/foo.py"})'),
             _result(2, "c0", 1011, "def foo():\n    return 1")]
    rows += [_call(3, "c1", 1020, 'exec_command({"cmd":"add docstring to foo()"})'),
             _result(4, "c1", 1021, "edited src/foo.py")]
    rows += [_call(5, "c2", 1030, 'exec_command({"cmd":"pytest tests/test_foo.py -x"})'),
             _result(6, "c2", 1031, "1 passed")]
    rows.append(_msg(7, "assistant", "已加 docstring，测试通过", 1040))
    return rows


def main(base_url: str, memory_root: str, log_path: str, run_tag: str = "") -> int:
    cases = [
        ("known-pass", known_pass_session(), "passed"),
        ("known-structural-reject", known_structural_reject_session(), "structural_reject"),
        ("known-semantic-reject", known_semantic_reject_session(), "semantic_reject"),
    ]
    prefix = f"calibrate-{run_tag}-" if run_tag else "calibrate-"
    all_ok = True
    for label, rows, expected in cases:
        session_id = f"{prefix}{label}"
        local_rounds = count_tool_rounds(rows)
        start_offset = os.path.getsize(log_path) if os.path.exists(log_path) else 0
        run_session(base_url, session_id, rows, AGENT_ID, USER_SENDER, clamper=make_clamper())
        time.sleep(15)   # M0 实测 /flush 返回到 markdown 落盘约 5-13s，留边际
        end_offset = os.path.getsize(log_path) if os.path.exists(log_path) else start_offset
        window = read_log_window(log_path, start_offset, end_offset)
        case_ids = []
        for f in find_session_case_files(memory_root, session_id):
            case_ids.extend(session_case_entry_ids(f.read_text(encoding="utf-8"), session_id))
        got = classify_session(window, case_ids)
        ok = got == expected
        all_ok = all_ok and ok
        print(f"[{label}] local_rounds={local_rounds} expected={expected} got={got} "
              f"{'OK' if ok else 'MISMATCH'}")
        reason_lines = [l for l in window.splitlines()
                        if "tool-call rounds" in l or "filtered out by LLM" in l]
        for rl in reason_lines:
            print(f"  EverOS log: {rl.strip()}")
    print("CALIBRATION", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    tag = sys.argv[4] if len(sys.argv) > 4 else ""
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3], tag))
