# scripts/everos_mcp_faults.py
"""Task 9:故障注入套件——spec §8 逐项验收(每项 PASS/FAIL,汇总退出码)。

全部案例跑在**隔离**环境下(mktemp 账本目录、ephemeral 端口、假 docker、假
Infinity、假 EverOS)——绝不碰生产 ledger/EverOS/Infinity/docker 容器。

案例清单(与任务简报逐项对应):
  1. accepted_write_fail  —— 工具 error + ops started/terminal(error) 在账
  2. ops_write_fail       —— 请求拒绝 + 进程退出码 86(子进程跑 server)
  3. kill_minus_9         —— 账一致(client-received hit/abstain 行仍在)
  4. double_instance      —— 第二实例因 flock 失败(fail-fast,不进入服务)
  5. redirect_stub        —— 第二个(canary)stub 零出站
  6. wedged_writer        —— late-commit 语义(error 先返回,原始行迟后落地,
                              response_aborted 追加在其后)
  7. journal_plaintext    —— 抓取的进程输出里不含查询原文
  8. session_churn        —— 回归用例:60 个连续一次性 MCP session 全部成功
                              (回归 fastmcp 3.4.2 默认有状态 session 永不清理、
                              最终把新 session 创建锁排到永久阻塞的真 bug——
                              见 server.py `stateless_http=True` 修复)
  9. pressure_backlog     —— 打分队列积压(慢 Infinity)下 p95<=空载p95*2 且
                              error 率不升
  10. pressure_backend_down—— 打分后端停摆下同一断言

用法:
    uv run --frozen --group mcp-shadow python -m scripts.everos_mcp_faults

退出码:0 = 全 PASS,1 = 有 FAIL。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import math
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from scripts import _everos_mcp_stubs as stubs

_ENV_PREFIXES = ("EVEROS_", "SHADOW_", "INFINITY_", "TELEGRAM_")


@dataclass
class CaseResult:
    name: str
    passed: bool
    detail: str
    extra: dict = field(default_factory=dict)


def _nearest_rank_percentile(samples: list[float], p: float) -> float:
    s = sorted(samples)
    n = len(s)
    rank = max(1, min(math.ceil(p / 100.0 * n), n))
    return s[rank - 1]


# ======================================================================
# 通用隔离环境搭建(每个案例独立一份,互不干扰)
# ======================================================================

class _Isolated:
    """一个案例专用的隔离拓扑:mktemp 根 + 假 docker + Infinity/EverOS stub。
    `env(**overrides)` 产出可直接喂给 `subprocess_env` 的公共部分。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="everos-mcp-fault-"))
        self.bin_dir = stubs.make_stub_docker(self.tmp / "bin")
        self.dirs = stubs.build_isolated_dirs(self.tmp / "iso")
        self.infinity = stubs.InfinityStub()
        self.everos = stubs.EverosStub()

    def env(self, *, fault=None, expect_empty: bool = False, **raw_overrides) -> dict:
        """`fault`/`expect_empty` 转发给 `subprocess_env`(它们要落地成
        `SHADOW_FAULT`/`EVEROS_MCP_EXPECT_EMPTY` 这两个具体 env key,不是字面量
        "fault"/"expect_empty" 键——直接 `dict.update` 会把这俩当成不存在的
        env var 悄悄漏掉,故障永远不生效)。`raw_overrides` 仍走原样 update,
        供极少数需要直接覆写具体 env key(如 PATH)的场景使用。"""
        port = stubs.free_port()
        base = stubs.subprocess_env(
            bin_dir=self.bin_dir, ledger_dir=self.dirs["ledger_dir"],
            instance_dir=self.dirs["instance_dir"], pin_file=self.dirs["pin_file"],
            everos_base=self.everos.base_url, infinity_base=self.infinity.base_url,
            port=port, traffic_class="fault_test", fault=fault, expect_empty=expect_empty,
        )
        base.update(raw_overrides)
        return base

    def close(self):
        self.infinity.shutdown()
        self.everos.shutdown()


# ======================================================================
# 1. SHADOW_FAULT=accepted_write_fail
# ======================================================================

def case_accepted_write_fail() -> CaseResult:
    iso = _Isolated()
    try:
        env = iso.env(fault="accepted_write_fail")
        port = int(env["EVEROS_MCP_PORT"])
        proc = stubs.spawn_server(env)
        try:
            res = stubs.wait_ready(port, env["EVEROS_MCP_TOKEN"], timeout=60, proc=proc)
        finally:
            rc, out = stubs.terminate_and_collect(proc)

        data = res.data
        if data.get("status") != "error" or data.get("meta", {}).get("error_code") != "ledger_unavailable":
            return CaseResult("accepted_write_fail", False,
                               f"期望 status=error/error_code=ledger_unavailable,实得 {data}")

        rid = data["meta"]["mcp_request_id"]
        from everos_mcp import ledger as ledger_mod  # noqa: PLC0415 —— 延迟导入,仅本案例需要

        ops_rows, _ = ledger_mod.iter_rows(iso.dirs["ledger_dir"], "ops")
        rid_ops = [r for r in ops_rows if r.get("rid") == rid]
        kinds = {r["kind"] for r in rid_ops}
        terminal = next((r for r in rid_ops if r["kind"] == "terminal"), None)
        if kinds != {"started", "terminal"} or terminal is None or terminal.get("effective_status") != "error":
            return CaseResult("accepted_write_fail", False,
                               f"ops 行不满足 started+terminal(error): {rid_ops}")

        abort_rids = ledger_mod.read_abort_rids(iso.dirs["ledger_dir"])
        if rid not in abort_rids:
            return CaseResult("accepted_write_fail", False, f"rid={rid} 未出现在 aborts.log")

        return CaseResult("accepted_write_fail", True,
                           "accepted 写入故障注入生效:工具返回 error/ledger_unavailable,"
                           "ops started+terminal(error) 在账,rid 已记入 aborts.log。")
    finally:
        iso.close()


# ======================================================================
# 2. SHADOW_FAULT=ops_write_fail —— 进程退出码 86
# ======================================================================

def case_ops_write_fail() -> CaseResult:
    iso = _Isolated()
    try:
        env = iso.env(fault="ops_write_fail")
        port = int(env["EVEROS_MCP_PORT"])
        proc = stubs.spawn_server(env)
        try:
            stubs.wait_tcp_ready(port, timeout=60, proc=proc)

            async def _call_with_timeout():
                return await asyncio.wait_for(
                    stubs._try_call(f"http://127.0.0.1:{port}/mcp", env["EVEROS_MCP_TOKEN"], "任意任务"),
                    timeout=10.0,
                )

            # 触发一次真实调用——ops started 写入必崩,预期本次调用本身收不到
            # 正常协议响应(连接被进程自杀中断),真正的断言是"进程退出码 86"。
            # 客户端侧显式套 10s 超时:进程自杀后 TCP 连接理应很快收到
            # EOF/reset,但不依赖这一点——超时兜底,不能让本案例挂死。
            try:
                asyncio.run(_call_with_timeout())
            except Exception:  # noqa: BLE001 —— 预期:进程中途自杀,连接必然异常终止或超时
                pass
            proc.wait(timeout=15)
        finally:
            rc, out = stubs.terminate_and_collect(proc, timeout=5)

        if rc != 86:
            return CaseResult("ops_write_fail", False, f"期望进程退出码 86,实得 {rc}\n{out[-2000:]}")
        return CaseResult("ops_write_fail", True,
                           "ops-started 写入故障注入生效:子进程以退出码 86(ops-fatal)终止。")
    finally:
        iso.close()


# ======================================================================
# 3. kill -9 后账一致
# ======================================================================

def case_kill_minus_9() -> CaseResult:
    iso = _Isolated()
    try:
        env = iso.env()
        port = int(env["EVEROS_MCP_PORT"])
        proc = stubs.spawn_server(env)
        rids = []
        try:
            first = stubs.wait_ready(port, env["EVEROS_MCP_TOKEN"], timeout=60, proc=proc)
            rids.append(first.data["meta"]["mcp_request_id"])

            async def _more_calls():
                client = stubs.Client(f"http://127.0.0.1:{port}/mcp", auth=env["EVEROS_MCP_TOKEN"])
                out = []
                async with client:
                    for i in range(4):
                        r = await client.call_tool("everos_search", {"task": f"kill-9 测试查询 {i}", "limit": 3})
                        out.append(r.data)
                return out

            more = asyncio.run(_more_calls())
            for r in more:
                assert r["status"] in ("hit", "abstain_empty"), r
                rids.append(r["meta"]["mcp_request_id"])
        finally:
            # `proc` 是 `uv run` 本身;SIGKILL 谁都转发不了,必须打整个进程组
            # 才能真的杀死持有 flock 的那个 `python -m everos_mcp.server`
            # (见 `spawn_server`/`killpg_hard` 文档字符串)。
            stubs.killpg_hard(proc)
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass

        from everos_mcp import ledger as ledger_mod  # noqa: PLC0415

        # 重开一个全新 Ledger 实例(无故障注入)必须成功——证明 flock 已随进程
        # 死亡释放、且没有残留半行把启动协议卡死(LedgerPermissionError/torn
        # tail 处理均需通过)。`proc.wait()` 只保证 `uv`(直接子进程)已被回收,
        # 不保证真正持锁的 grandchild(`python -m everos_mcp.server`,SIGKILL
        # 直接发给它,不经过 uv 转发)在同一时刻已经完全释放 fd——留一个短
        # 重试窗口容忍这个内核调度层面的极小时间差,而不是假装它总是瞬时的。
        fresh = None
        last_locked_exc = None
        for _attempt in range(20):
            try:
                fresh = ledger_mod.Ledger(iso.dirs["ledger_dir"])
                break
            except ledger_mod.LedgerLocked as e:
                last_locked_exc = e
                time.sleep(0.1)
        if fresh is None:
            return CaseResult("kill_minus_9", False,
                               f"kill -9 后 2s 内 ledger flock 仍未释放: {last_locked_exc}")
        try:
            ops_rows, _ = fresh.iter_rows("ops")
            accepted_rows, _ = fresh.iter_rows("accepted")
        finally:
            fresh.close(drain=False)

        missing = []
        for rid in rids:
            terminal = next(
                (r for r in ops_rows if r.get("rid") == rid and r.get("kind") == "terminal"), None
            )
            accepted = next(
                (r for r in accepted_rows if r.get("rid") == rid and r.get("kind") == "accepted"), None
            )
            if terminal is None or accepted is None:
                missing.append(rid)
        if missing:
            return CaseResult("kill_minus_9", False,
                               f"client 已收到响应的 rid 里,以下在 kill -9 后账本缺行: {missing}")

        return CaseResult("kill_minus_9", True,
                           f"kill -9 后重开 Ledger 无异常;{len(rids)} 个 client-received "
                           "rid 的 ops terminal + accepted 行均完整可读。")
    finally:
        iso.close()


# ======================================================================
# 4. 双实例——flock 拒绝第二实例
# ======================================================================

def case_double_instance() -> CaseResult:
    iso = _Isolated()
    try:
        env_a = iso.env()
        port_a = int(env_a["EVEROS_MCP_PORT"])
        proc_a = stubs.spawn_server(env_a)
        proc_b = None
        try:
            stubs.wait_ready(port_a, env_a["EVEROS_MCP_TOKEN"], timeout=60, proc=proc_a)

            env_b = dict(env_a)
            env_b["EVEROS_MCP_PORT"] = str(stubs.free_port())  # 端口不同,ledger_dir 相同
            proc_b = stubs.spawn_server(env_b)
            try:
                rc_b = proc_b.wait(timeout=30)
            except Exception:
                stubs.killpg_hard(proc_b)
                rc_b = proc_b.wait(timeout=10)
            out_b = stubs.collected_output(proc_b)  # stdout 由排空线程收集,不直接 read（会与线程抢管道）

            if rc_b == 0:
                return CaseResult("double_instance", False, "第二实例不应该成功启动(rc==0)")
            if "LedgerLocked" not in out_b:
                return CaseResult("double_instance", False,
                                   f"第二实例失败但输出未见 LedgerLocked 证据: {out_b[-2000:]}")
            return CaseResult("double_instance", True,
                               f"第二实例指向同一 ledger_dir 时因 flock 竞争 fail-fast(rc={rc_b}),"
                               "输出含 LedgerLocked 证据;首实例期间持续正常服务。")
        finally:
            stubs.terminate_and_collect(proc_a)
            if proc_b is not None and proc_b.poll() is None:
                stubs.killpg_hard(proc_b)
    finally:
        iso.close()


# ======================================================================
# 5. redirect stub —— 第二个(canary)stub 零出站
# ======================================================================

def case_redirect_stub() -> CaseResult:
    iso = _Isolated()
    canary = stubs.EverosStub()
    try:
        canary_hits_before = canary.state.request_count()
        iso.everos.state.mode = "redirect"
        iso.everos.state.redirect_target = canary.base_url + "/dest"

        env = iso.env(expect_empty=True)  # 跳过启动探针(探针本身也会撞上重定向)
        port = int(env["EVEROS_MCP_PORT"])
        proc = stubs.spawn_server(env)
        try:
            res = stubs.wait_ready(port, env["EVEROS_MCP_TOKEN"], timeout=60, proc=proc)
        finally:
            stubs.terminate_and_collect(proc)

        data = res.data
        canary_hits_after = canary.state.request_count()

        if data.get("status") != "error":
            return CaseResult("redirect_stub", False, f"期望 status=error(拒绝重定向),实得 {data}")
        if canary_hits_after != canary_hits_before:
            return CaseResult("redirect_stub", False,
                               f"canary stub 收到了 {canary_hits_after - canary_hits_before} 次出站请求,期望 0")
        return CaseResult("redirect_stub", True,
                           f"EverOS 302 重定向被 opener 层拒绝(status=error,"
                           f"error_code={data['meta']['error_code']!r}),canary stub 零出站。")
    finally:
        canary.shutdown()
        iso.close()


# ======================================================================
# 6. 挂 writer —— late-commit 语义(in-process,直接操纵 accepted writer 的
#    底层文件句柄制造"卡住"效果——子进程里做不到跨进程 monkeypatch)
# ======================================================================

class _SlowFileProxy:
    """代理真实文件对象:第一次 `write()` 调用前先 sleep `delay` 秒,模拟
    writer 卡住(之后的调用不再延迟——单个测试 rid 只触发一次写入)。"""

    def __init__(self, real_fh, delay: float):
        self._real = real_fh
        self._delay = delay
        self._first = True

    def write(self, data):
        if self._first:
            self._first = False
            time.sleep(self._delay)
        return self._real.write(data)

    def flush(self):
        return self._real.flush()

    def fileno(self):
        return self._real.fileno()

    def close(self):
        return self._real.close()


def _set_env(env: dict) -> dict:
    """设置 os.environ(清掉旧的 EVEROS_/SHADOW_/INFINITY_/TELEGRAM_ 前缀 key,
    再灌入新值),返回恢复用的快照。"""
    snapshot = dict(os.environ)
    for k in list(os.environ):
        if k.startswith(_ENV_PREFIXES):
            del os.environ[k]
    os.environ.update(env)
    return snapshot


def _restore_env(snapshot: dict) -> None:
    os.environ.clear()
    os.environ.update(snapshot)


def case_wedged_writer() -> CaseResult:
    """P2(R4 阻断项 #7):writer 卡住 `_WEDGE_DELAY_S`(12s,远超单次 submit
    timeout 5s)——此前 accepted 回执超时之后,response_aborted 追加行与
    (accepted 失败分支触发的)ops terminal 都用**阻塞** `submit(timeout=5.0)`
    提交,worst-case handler 延迟因此从"一次 5s 超时"堆叠成"两次相加 ~10s"。
    修复后两者改用 `submit_nowait`(fire-and-forget,仍走同一条队列保证
    FIFO),worst-case handler 延迟应稳定在 ~5s+ε,不随 writer 实际卡住多久
    (哪怕卡 12s、卡 12min)而线性增长——这正是本用例故意把卡住时长设得远超
    "两次阻塞相加"的 ~10s 上限的原因:若 handler 延迟仍随卡住时长增长,说明
    某处还在阻塞等这次卡住的 writer。

    调用返回之后 writer 仍在长睡眠里,原始 accepted 行 + 迟到的
    response_aborted 行要等 writer 真正解卡(`_WEDGE_DELAY_S` 之后)才落盘
    ——late-commit 语义没变,只是"调用返回"与"数据落盘"彻底解耦,必须轮询
    等待,不能像修复前那样指望阻塞 submit 顺带当一次同步屏障。"""
    iso = _Isolated()
    old_path = os.environ.get("PATH", "")
    snapshot = None
    server_mod = None
    try:
        env = iso.env()
        env["PATH"] = f"{iso.bin_dir}:{old_path}"  # in-process 也走假 docker(真实 subprocess.run 调用)
        env.pop("EVEROS_MCP_PORT", None)  # in-process bootstrap 不需要真的监听端口
        snapshot = _set_env(env)
        os.environ["EVEROS_MCP_PORT"] = "1"  # config.load 仍要求该 key 存在(值本身不使用)

        import everos_mcp.server as server_mod  # noqa: PLC0415

        importlib.reload(server_mod)
        state = server_mod.bootstrap(skip_probe=True, start_watchdog=False)

        wedge_delay = 12.0  # 远超"两次 5s 阻塞相加"的 ~10s 上限——见函数文档
        state.ledger.accepted._fh = _SlowFileProxy(state.ledger.accepted._fh, delay=wedge_delay)

        task = f"wedged writer late-commit 测试 {uuid.uuid4().hex[:8]}"
        t0 = time.monotonic()
        result = server_mod.everos_search(task, 5)
        elapsed = time.monotonic() - t0

        if result.get("status") != "error" or result.get("meta", {}).get("error_code") != "ledger_timeout":
            return CaseResult("wedged_writer", False, f"期望 status=error/ledger_timeout,实得 {result}")

        # worst-case handler 延迟必须稳定在 ~5s+ε,不随 wedge_delay(12s)增长
        # ——修复前(response_aborted/ops terminal 补偿写都阻塞)这里会量到
        # ~10s;留够抖动余量,门槛设在 7s(< wedge_delay 的一半,足以分辨两种
        # 实现)。
        if elapsed >= 7.0:
            return CaseResult(
                "wedged_writer", False,
                f"handler 延迟 {elapsed:.1f}s >= 7.0s(wedge_delay={wedge_delay}s)——"
                "补偿性写入(response_aborted/ops terminal)疑似仍在堆叠阻塞等待,"
                "不是 best-effort nowait。",
            )

        rid = result["meta"]["mcp_request_id"]

        from everos_mcp import ledger as ledger_mod  # noqa: PLC0415

        deadline = time.monotonic() + wedge_delay + 8.0
        rid_rows: list = []
        while time.monotonic() < deadline:
            accepted_rows, _ = ledger_mod.iter_rows(iso.dirs["ledger_dir"], "accepted")
            rid_rows = [r for r in accepted_rows if r.get("rid") == rid]
            if len(rid_rows) >= 2:
                break
            time.sleep(0.2)

        if len(rid_rows) != 2:
            return CaseResult("wedged_writer", False,
                               f"期望 rid={rid} 在 accepted 流恰好 2 行(原始行+response_aborted),实得 {rid_rows}")
        if rid_rows[0].get("kind") != "accepted" or rid_rows[1].get("kind") != "response_aborted":
            return CaseResult("wedged_writer", False,
                               f"期望顺序 [accepted(原始行,迟到), response_aborted],实得 kind 序列 "
                               f"{[r.get('kind') for r in rid_rows]}")

        return CaseResult("wedged_writer", True,
                           f"writer 卡住 {wedge_delay:.0f}s(远超 submit timeout 5s)期间:调用在 "
                           f"{elapsed:.1f}s 后返回 error/ledger_timeout(非 10-15s 的堆叠阻塞);"
                           "writer 解卡后原始 accepted 行迟到落盘,response_aborted 行在其后追加"
                           "——late-commit 顺序符合预期。")
    finally:
        try:
            if server_mod is not None and server_mod._STATE is not None:
                server_mod._STATE.worker.close(drain=False)
                server_mod._STATE.ledger.close(drain=False)
        except Exception:  # noqa: BLE001
            pass
        if snapshot is not None:
            _restore_env(snapshot)
        iso.close()


# ======================================================================
# 7. journal/log 明文抽查
# ======================================================================

def case_journal_plaintext() -> CaseResult:
    iso = _Isolated()
    try:
        env = iso.env()
        port = int(env["EVEROS_MCP_PORT"])
        proc = stubs.spawn_server(env)
        secret = f"敏感查询明文标记-{uuid.uuid4().hex}"
        try:
            stubs.wait_ready(port, env["EVEROS_MCP_TOKEN"], timeout=60, proc=proc)

            async def _call():
                client = stubs.Client(f"http://127.0.0.1:{port}/mcp", auth=env["EVEROS_MCP_TOKEN"])
                async with client:
                    return await client.call_tool("everos_search", {"task": secret, "limit": 3})

            res = asyncio.run(_call())
            assert secret not in json.dumps(res.data, ensure_ascii=False), \
                "响应体本身就不该带查询原文(先在这里自证一次,再查进程输出)"
        finally:
            rc, out = stubs.terminate_and_collect(proc)

        if secret in out:
            return CaseResult("journal_plaintext", False,
                               f"进程捕获输出(stdout/stderr)含查询原文标记 {secret!r}")
        return CaseResult("journal_plaintext", True,
                           "响应体与进程捕获的全部 stdout/stderr 输出均不含查询原文标记。")
    finally:
        iso.close()


# ======================================================================
# 8. session churn —— 回归用例(Task 9 codex 复审抓出的真 bug):fastmcp 3.4.2
#    默认有状态 streamable-http session 永不清理(`StreamableHTTPSessionManager`
#    只在配置 `session_idle_timeout` 时才回收断开的 session,fastmcp 未透传该
#    参数),每次客户端重连都会在事件循环里永久攒一个卡在 `app.run()` 的僵尸
#    协程,最终把 `_session_creation_lock` 排到永久阻塞——线程数/fd 数全程
#    持平(不是 anyio 线程池耗尽),只有 session 数单调只增。修复:
#    `server.main()` 对 `mcp.run(...)` 传 `stateless_http=True`(每请求换新
#    transport,零 session 追踪,`everos_search` 是纯函数+模块级状态,语义无损)。
#    本用例连续开 60 个"一次性"session(仅 1 次调用即断开),断言全部成功,
#    且收尾时服务器仍能正常应答——防止这个修复被后续改动悄悄回退。
# ======================================================================

async def _one_shot_session(url: str, token: str, i: int) -> str:
    client = stubs.Client(url, auth=token)
    async with client:
        res = await client.call_tool("everos_search", {"task": f"session-churn-回归查询-{i:02d}", "limit": 3})
        return res.data.get("status")


async def _run_session_churn(url: str, token: str, count: int) -> list[dict]:
    results = []
    for i in range(count):
        t0 = time.monotonic()
        try:
            status = await asyncio.wait_for(_one_shot_session(url, token, i), timeout=10.0)
            results.append({"i": i, "ok": True, "status": status, "ms": (time.monotonic() - t0) * 1000.0})
        except Exception as e:  # noqa: BLE001 —— 记录失败,不中断循环(要看清楚是哪个 session 开始失败)
            results.append({"i": i, "ok": False, "error": f"{type(e).__name__}: {e}",
                             "ms": (time.monotonic() - t0) * 1000.0})
    return results


def case_session_churn() -> CaseResult:
    """回归 Task 9 codex 复审发现的 session 泄漏 bug:60 个连续一次性 session
    (每个只发 1 次调用即断开)必须全部成功,且收尾再打一次确认服务器仍在
    正常应答(不是"前面全过、其实已经悄悄卡住"的假阳性)。"""
    iso = _Isolated()
    try:
        env = iso.env()
        port = int(env["EVEROS_MCP_PORT"])
        url = f"http://127.0.0.1:{port}/mcp"
        proc = stubs.spawn_server(env)
        try:
            stubs.wait_ready(port, env["EVEROS_MCP_TOKEN"], timeout=60, proc=proc)
            churn_results = asyncio.run(_run_session_churn(url, env["EVEROS_MCP_TOKEN"], 60))
            failed = [r for r in churn_results if not r["ok"]]
            if failed:
                first_fail = failed[0]
                return CaseResult(
                    "session_churn", False,
                    f"{len(failed)}/60 个一次性 session 失败,首个失败在 session={first_fail['i']}: "
                    f"{first_fail['error']}(疑似 session 泄漏回归——见 stateless_http=True 修复)",
                )

            # 收尾再验证一次:确认 60 轮churn之后服务器仍然正常应答,不是
            # "前面全部凑巧过、其实已经卡住"的假阳性。
            final = asyncio.run(asyncio.wait_for(
                _one_shot_session(url, env["EVEROS_MCP_TOKEN"], 999), timeout=10.0,
            ))
            if final not in ("hit", "abstain_empty"):
                return CaseResult("session_churn", False, f"60 轮 churn 后收尾调用状态异常: {final!r}")
        finally:
            stubs.terminate_and_collect(proc)

        max_ms = max(r["ms"] for r in churn_results)
        return CaseResult(
            "session_churn", True,
            f"连续 60 个一次性 MCP session 全部成功(最慢单次 {max_ms:.0f}ms),"
            "收尾确认调用仍正常应答——session 泄漏回归未复现。",
        )
    finally:
        iso.close()


# ======================================================================
# 9/10. 压力门:打分队列积压(慢 Infinity)/ 打分后端停摆
# ======================================================================

async def _concurrent_calls(url: str, token: str, *, concurrency: int, calls_per_worker: int) -> list[dict]:
    async def _worker(worker_idx: int) -> list[dict]:
        client = stubs.Client(url, auth=token)
        out = []
        async with client:
            for i in range(calls_per_worker):
                t0 = time.perf_counter()
                res = await client.call_tool(
                    "everos_search",
                    {"task": f"压力门测试查询 w{worker_idx}-{i}", "limit": 3},
                )
                dt_ms = (time.perf_counter() - t0) * 1000.0
                out.append({"ms": dt_ms, "status": res.data.get("status")})
        return out

    results = await asyncio.gather(*(_worker(i) for i in range(concurrency)))
    flat: list[dict] = []
    for r in results:
        flat.extend(r)
    return flat


def _phase_stats(samples: list[dict]) -> dict:
    ms = [s["ms"] for s in samples]
    errors = [s for s in samples if s["status"] not in ("hit", "abstain_empty")]
    return {
        "p95_ms": _nearest_rank_percentile(ms, 95),
        "error_rate": len(errors) / len(samples) if samples else 1.0,
        "n": len(samples),
    }


def _pressure_gate_cases() -> list[CaseResult]:
    iso = _Isolated()
    concurrency, calls_per_worker = 5, 6
    try:
        env = iso.env()
        port = int(env["EVEROS_MCP_PORT"])
        proc = stubs.spawn_server(env)
        try:
            stubs.wait_ready(port, env["EVEROS_MCP_TOKEN"], timeout=60, proc=proc)
            url = f"http://127.0.0.1:{port}/mcp"

            idle_samples = asyncio.run(_concurrent_calls(url, env["EVEROS_MCP_TOKEN"],
                                                          concurrency=concurrency,
                                                          calls_per_worker=calls_per_worker))
            idle = _phase_stats(idle_samples)

            iso.infinity.state.mode = "slow"
            iso.infinity.state.slow_seconds = 2.0
            backlog_samples = asyncio.run(_concurrent_calls(url, env["EVEROS_MCP_TOKEN"],
                                                              concurrency=concurrency,
                                                              calls_per_worker=calls_per_worker))
            backlog = _phase_stats(backlog_samples)

            iso.infinity.shutdown()  # 打分后端彻底停摆
            down_samples = asyncio.run(_concurrent_calls(url, env["EVEROS_MCP_TOKEN"],
                                                           concurrency=concurrency,
                                                           calls_per_worker=calls_per_worker))
            down = _phase_stats(down_samples)
        finally:
            stubs.terminate_and_collect(proc)

        results = []
        for name, phase in (("pressure_backlog", backlog), ("pressure_backend_down", down)):
            gate_p95 = phase["p95_ms"] <= idle["p95_ms"] * 2
            gate_err = phase["error_rate"] <= idle["error_rate"]
            passed = gate_p95 and gate_err
            detail = (
                f"idle: p95={idle['p95_ms']:.1f}ms err={idle['error_rate']:.2%} (n={idle['n']}); "
                f"{name}: p95={phase['p95_ms']:.1f}ms err={phase['error_rate']:.2%} (n={phase['n']}); "
                f"gate p95<=idle*2: {gate_p95}; gate err 不升: {gate_err}"
            )
            results.append(CaseResult(name, passed, detail,
                                       extra={"idle": idle, "phase": phase}))
        return results
    finally:
        iso.close()


# ======================================================================
# 编排
# ======================================================================

_SIMPLE_CASES: list[Callable[[], CaseResult]] = [
    case_accepted_write_fail,
    case_ops_write_fail,
    case_kill_minus_9,
    case_double_instance,
    case_redirect_stub,
    case_wedged_writer,
    case_journal_plaintext,
    case_session_churn,
]


def main() -> int:
    results: list[CaseResult] = []
    for fn in _SIMPLE_CASES:
        name = fn.__name__.removeprefix("case_")
        t0 = time.monotonic()
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001 —— 单个案例异常不能打断整套suite
            r = CaseResult(name, False, f"案例执行抛出未捕获异常: {type(e).__name__}: {e}")
        elapsed = time.monotonic() - t0
        print(f"[{'PASS' if r.passed else 'FAIL'}] {r.name} ({elapsed:.1f}s) — {r.detail}", flush=True)
        results.append(r)

    try:
        results.extend(_pressure_gate_cases())
    except Exception as e:  # noqa: BLE001
        results.append(CaseResult("pressure_gates", False, f"压力门案例抛出未捕获异常: {e}"))
    for r in results[len(_SIMPLE_CASES):]:
        print(f"[{'PASS' if r.passed else 'FAIL'}] {r.name} — {r.detail}", flush=True)

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\n=== everos_mcp faults suite: {passed}/{total} PASS ===", flush=True)
    for r in results:
        if not r.passed:
            print(f"  FAIL: {r.name}: {r.detail}", flush=True)

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
