"""bench/faults 共享基座(`scripts/_everos_mcp_stubs.py`)的回归测试。

锁定 Task 9 systematic-debugging 定位的根因:server 子进程以 `stdout=PIPE` 起,
uvicorn 对每个 HTTP 请求都会在事件循环线程内**同步**写一行 access log 到 stdout;
父进程若不持续排空这个管道,写满内核默认 64KiB 管道缓冲后,事件循环线程会阻塞在
`logging.flush()`,整个 server 卡死(不再 accept、连接停 CLOSE-WAIT)。修复是
`spawn_server` 挂一个后台线程持续排空 stdout。本测试用一个"猛写 stdout 再退出"的
通用子进程直接验证排空机制:不排空则子进程会阻塞在 write 永不退出、`wait` 超时;
排空则子进程正常跑完退出。
"""
from __future__ import annotations

import subprocess
import sys
import time

from scripts import _everos_mcp_stubs as stubs


def test_stdout_drainer_prevents_pipe_fill_deadlock():
    # 子进程写约 480KiB 到 stdout(远超默认 64KiB 管道缓冲),再打印 DONE 后退出。
    # 若父进程不排空,子进程会在管道写满时阻塞在 write() 上、永远走不到退出,
    # `proc.wait(timeout=...)` 因此超时 —— 那正是修复前 server 事件循环的死法。
    child = (
        "import sys\n"
        "for _ in range(8000):\n"
        "    sys.stdout.write('x' * 60 + '\\n')\n"
        "sys.stdout.write('DONE\\n')\n"
        "sys.stdout.flush()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    stubs._attach_stdout_drainer(proc)

    # 排空线程在跑 -> 子进程不会因管道写满而阻塞,能迅速正常退出。
    rc = proc.wait(timeout=30)
    assert rc == 0, f"子进程未正常退出(rc={rc})——排空线程可能没生效"

    out = stubs.collected_output(proc)
    assert "DONE" in out, "排空线程应收集到子进程 stdout 的末尾行 DONE"


def test_terminate_and_collect_returns_drained_tail():
    # 一个只写少量行然后 sleep 的子进程:验证 terminate_and_collect 走排空线程路径
    # (不再用 communicate())也能拿到已产出的 stdout tail,并干净终止进程。
    child = (
        "import sys, time\n"
        "sys.stdout.write('MARKER-LINE\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    stubs._attach_stdout_drainer(proc)
    # 等排空线程真的收到 MARKER-LINE 再终止(真实用法里 terminate 永远发生在
    # server 已 wait_ready、早已产出大量 stdout 之后;这里等一下避免 SIGTERM
    # 抢在解释器写第一行之前到达的人造竞态)。
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "MARKER-LINE" in stubs.collected_output(proc, join_timeout=0.0):
            break
        time.sleep(0.05)
    # 重点:能在 timeout 内返回(不挂)+ 拿到已产出的 tail;进程确已终止。
    rc, out = stubs.terminate_and_collect(proc, timeout=10.0)
    assert proc.poll() is not None, "terminate_and_collect 后子进程应已终止"
    assert "MARKER-LINE" in out
