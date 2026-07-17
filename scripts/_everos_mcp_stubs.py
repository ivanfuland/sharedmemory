# scripts/_everos_mcp_stubs.py
"""共享测试基座:everos_mcp_bench.py / everos_mcp_faults.py 共用。

Task 9 的验收工具(bench + 故障注入套件)都需要在**完全隔离**的环境下跑一个
真实的 `python -m everos_mcp.server` 子进程或真实 `everos_mcp.server.bootstrap()`
——绝不碰生产 ledger 目录、生产 EverOS、生产 Infinity、生产 docker 容器。

本模块提供:
- `make_stub_docker(bin_dir)`:写一个假 `docker` 可执行文件到 `bin_dir/docker`,
  响应 `everos_mcp.scorer._run_docker` 会发出的四类调用(inspect .Config.Image /
  .Image / .State.StartedAt、image inspect、exec sh -c 遍历 HF 缓存)——固定
  返回值,永不漂移(marker 双取判定天然一致,除非调用方自己改)。
- `InfinityStub` / `EverosStub`:与 `tests/everos_mcp/test_server.py` 同款
  `http.server` 桩,额外支持本任务需要的模式切换(慢响应、断线、重定向、
  逐查询唯一内容)。
- `free_port()` / `build_isolated_dirs()` / `subprocess_env()`:隔离拓扑组装
  (ephemeral 端口、mktemp 目录、合成 token/agent_id,零真实拓扑字面量)。
- `spawn_server()` / `wait_ready()`:起真实子进程 + 轮询直到能真正应答一次
  `everos_search` 调用(不是端口能连就算,是协议层真正握手成功)。

PUBLIC 仓纪律:本文件里的容器名/端口/token 全部是本模块生成的合成占位值。
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import http.server as http_server_mod
import json
import os
import signal
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from fastmcp.client import Client

REPO_ROOT = Path(__file__).resolve().parents[1]

_DOCKER_STUB_SCRIPT = """#!/usr/bin/env bash
# fake docker for isolated everos_mcp bench/fault runs — never touches real docker.
set -euo pipefail
CONFIG_IMAGE="example.invalid/everos-mcp-stub-infinity@sha256:$(printf 'a%.0s' $(seq 1 64))"
CONTAINER_IMAGE="sha256:$(printf 'b%.0s' $(seq 1 64))"
STARTED_AT="2026-07-17T00:00:00.000000000Z"
case "${1:-}" in
  inspect)
    fmt="${4:-}"
    case "$fmt" in
      '{{.Config.Image}}') echo "$CONFIG_IMAGE" ;;
      '{{.Image}}') echo "$CONTAINER_IMAGE" ;;
      '{{.State.StartedAt}}') echo "$STARTED_AT" ;;
      *) echo "fake docker: unsupported inspect format: $fmt" >&2; exit 1 ;;
    esac
    ;;
  image)
    echo "$CONFIG_IMAGE"
    ;;
  exec)
    printf '%s  /app/.cache/huggingface/hub/models--BAAI--bge-m3/blobs/config-v1\\n' \\
      "$(printf 'c%.0s' $(seq 1 64))"
    printf '%s  /app/.cache/huggingface/hub/models--BAAI--bge-m3/blobs/weight-v1\\n' \\
      "$(printf 'd%.0s' $(seq 1 64))"
    ;;
  *)
    echo "fake docker: unsupported command: $*" >&2
    exit 1
    ;;
esac
"""


def make_stub_docker(bin_dir: Path) -> Path:
    """写假 `docker` 到 `bin_dir/docker`,返回 `bin_dir`(调用方把它 prepend 进
    子进程 PATH)。固定输出、永不漂移——`scorer.py` 的 marker 双取判定
    (采集前后 `.Image`+`.State.StartedAt` 必须一致)天然满足。"""
    bin_dir.mkdir(parents=True, exist_ok=True)
    docker_path = bin_dir / "docker"
    docker_path.write_text(_DOCKER_STUB_SCRIPT, encoding="utf-8")
    docker_path.chmod(docker_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def free_port() -> int:
    """绑 127.0.0.1:0 拿一个当前空闲端口再立刻放掉——单机短生命周期脚本的
    标准手法,brief 允许的"else 运行时挑空闲端口"路径(uvicorn 走同步 `run()`,
    不支持程序化取回它绑定的实际端口,不能直接传 0)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ======================================================================
# Infinity stub(/models、/embeddings、/rerank)—— 可切换 fast/slow/down
# ======================================================================

def _embed_vec(text: str) -> list:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [(b + 1) / 256.0 for b in h[:8]]


def _rerank_score(query: str, doc: str) -> float:
    h = hashlib.sha256((query + "\x00" + doc).encode("utf-8")).digest()
    return h[0] / 255.0


class _InfinityState:
    def __init__(self):
        self.mode = "fast"  # fast | slow
        self.slow_seconds = 3.0
        self.request_count = 0
        self.lock = threading.Lock()

    def record(self):
        with self.lock:
            self.request_count += 1


class _InfinityHandler(http_server_mod.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _maybe_delay(self):
        state: _InfinityState = self.server.state
        if state.mode == "slow":
            time.sleep(state.slow_seconds)

    def do_GET(self):  # noqa: N802
        state: _InfinityState = self.server.state
        state.record()
        if self.path == "/models":
            self._maybe_delay()
            self._json(200, {"data": [{"id": "BAAI/bge-m3"}, {"id": "BAAI/bge-reranker-v2-m3"}]})
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        state: _InfinityState = self.server.state
        state.record()
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        self._maybe_delay()
        if self.path == "/embeddings":
            texts = payload["input"]
            data = [{"index": i, "embedding": _embed_vec(t)} for i, t in enumerate(texts)]
            self._json(200, {"data": data})
            return
        if self.path == "/rerank":
            query = payload["query"]
            docs = payload["documents"]
            results = [
                {"index": i, "relevance_score": _rerank_score(query, d)} for i, d in enumerate(docs)
            ]
            self._json(200, {"results": results})
            return
        self.send_error(404)


class InfinityStub:
    def __init__(self):
        self.server = http_server_mod.ThreadingHTTPServer(("127.0.0.1", 0), _InfinityHandler)
        self.server.state = _InfinityState()
        self.state = self.server.state
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:  # noqa: BLE001 —— 关闭桩本身失败不影响调用方后续判定
            pass


# ======================================================================
# EverOS stub(/api/v1/memory/search)—— 逐查询唯一内容 / 重定向 / 慢响应
# ======================================================================

def _unique_envelope_for_query(query: str) -> dict:
    """内容按查询文本确定性生成——同一查询文本永远得到同一份候选内容(内容
    寻址的 blobstore 因而在重复查询上天然命中缓存),不同查询文本天然得到不同
    候选内容(bench 的"逐查询唯一"要求由此满足,不需要额外簿记哪一轮已经
    发过)。"""
    h = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return {
        "request_id": f"req-{h[:16]}",
        "data": {
            "agent_cases": [
                {
                    "id": f"ac_{h[:16]}_{i}",
                    "score": 0.9 - i * 0.04,
                    "task_intent": f"合成任务意图 {h[:8]} #{i}",
                    "approach": f"合成解法摘要 {h[8:16]} #{i}",
                }
                for i in range(20)  # 简报冻结值:20+20(不是 10+10)——bench 门禁按这个候选量算
            ],
            "agent_skills": [
                {
                    "id": f"sk_{h[:16]}_{i}",
                    "score": 0.85 - i * 0.04,
                    "name": f"合成技能名 {h[:8]} #{i}",
                    "description": f"合成技能描述 {h[8:16]} #{i}",
                }
                for i in range(20)
            ],
        },
    }


class _EverosState:
    def __init__(self):
        self.mode = "unique"  # unique | redirect | down(down 由调用方直接 shutdown 实现)
        self.redirect_target: Optional[str] = None
        self.requests: list[str] = []
        self.lock = threading.Lock()

    def record(self, query: str) -> None:
        with self.lock:
            self.requests.append(query)

    def request_count(self) -> int:
        with self.lock:
            return len(self.requests)


class _EverosHandler(http_server_mod.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def do_POST(self):  # noqa: N802
        state: _EverosState = self.server.state
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            payload = {}
        query = payload.get("query", "")
        state.record(query)

        if state.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", state.redirect_target or "http://127.0.0.1:1/dest")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        envelope = _unique_envelope_for_query(query)
        body = json.dumps(envelope).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class EverosStub:
    def __init__(self):
        self.server = http_server_mod.ThreadingHTTPServer(("127.0.0.1", 0), _EverosHandler)
        self.server.state = _EverosState()
        self.state = self.server.state
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:  # noqa: BLE001
            pass


# ======================================================================
# 隔离目录 + env 组装
# ======================================================================

def build_isolated_dirs(tmp_root: Path) -> dict:
    """`tmp_root` 下建 ledger_dir(0700)/ instance_dir(.cases + skills 各一份
    最小合法内容)/ pin_file。返回三个 Path 的字典。"""
    ledger_dir = tmp_root / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_dir.chmod(0o700)

    instance_dir = tmp_root / "instance"
    (instance_dir / ".cases").mkdir(parents=True, exist_ok=True)
    (instance_dir / "skills" / "demo-skill").mkdir(parents=True, exist_ok=True)
    (instance_dir / "skills" / "demo-skill" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    (instance_dir / ".cases" / "agent_case-2026-07-17.md").write_text(
        "---\nentry_count: 1\n---\n# cases\n", encoding="utf-8"
    )

    pin_file = tmp_root / "PIN"
    pin_file.write_text("git_sha=deadbeef\nfreeze_hash=cafef00d\n", encoding="utf-8")

    return {"ledger_dir": ledger_dir, "instance_dir": instance_dir, "pin_file": pin_file}


def subprocess_env(
    *,
    bin_dir: Path,
    ledger_dir: Path,
    instance_dir: Path,
    pin_file: Path,
    everos_base: str,
    infinity_base: str,
    port: int,
    token: str = "synthetic-isolated-token",
    agent_id: str = "synthetic-isolated-agent",
    container: str = "synthetic-isolated-infinity-container",
    traffic_class: Optional[str] = None,
    fault: Optional[str] = None,
    expect_empty: bool = False,
) -> dict:
    """组装隔离子进程的**完整** env——不是父进程 env 的拷贝(白名单式构造,
    避免父进程无关变量泄漏进子进程,同 MEMORY.md spawn env 白名单教训)。"""
    home = os.environ.get("HOME", "/root")
    env = {
        # bin_dir 打头:假 docker 必须优先于任何真 docker 被解析到。
        # `%h/.local/bin` 是 uv 的标准安装位置(与生产 unit `ExecStart=%h/.local/bin/uv`
        # 同一约定)——PATH 解析而非硬编码绝对路径,方便脚本在不同宿主机复用。
        "PATH": f"{bin_dir}:{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin",
        "HOME": home,
        "EVEROS_MCP_PORT": str(port),
        "EVEROS_MCP_TOKEN": token,
        "EVEROS_BASE_URL": everos_base,
        "EVEROS_AGENT_ID": agent_id,
        "INFINITY_BASE": infinity_base,
        "SHADOW_LEDGER_DIR": str(ledger_dir),
        "EVEROS_EMBED_MODEL": "BAAI/bge-m3",
        "EVEROS_RERANK_MODEL": "BAAI/bge-reranker-v2-m3",
        "EVEROS_PIN_FILE": str(pin_file),
        "EVEROS_INSTANCE_DIR": str(instance_dir),
        "INFINITY_CONTAINER": container,
    }
    # HF/uv 缓存位置依赖 HOME 展开;显式透传常见缓存覆盖点,避免子进程另起炉灶
    # 重新下载/重新解析已缓存的 pinned tokenizer。
    for k in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "UV_CACHE_DIR", "XDG_CACHE_HOME"):
        if k in os.environ:
            env[k] = os.environ[k]
    if traffic_class:
        env["SHADOW_TRAFFIC_CLASS"] = traffic_class
    if fault:
        env["SHADOW_FAULT"] = fault
    if expect_empty:
        env["EVEROS_MCP_EXPECT_EMPTY"] = "1"
    return env


_STDOUT_SINK_ATTR = "_everos_stdout_sink"
_STDOUT_DRAINER_ATTR = "_everos_stdout_drainer"
_STDOUT_SINK_MAXLEN = 8000  # 保留最近 N 行(足够覆盖任一故障用例的诊断 tail)


def _attach_stdout_drainer(proc: subprocess.Popen) -> None:
    """给子进程挂一个**持续**排空 stdout 的后台读线程。

    根因(Task 9 systematic-debugging faulthandler 定位,见 task-9-rootcause.md):
    server 子进程以 `stdout=PIPE` 起,uvicorn 对**每个** HTTP 请求都会在事件循环
    线程内同步写一行 access log 到 stdout。若父进程不持续读这个管道,写满内核
    默认 64KiB 管道缓冲后(约 1100 行 access log ≈ bench 第 34 轮),事件循环
    线程会**阻塞在 `logging` 的 `flush()`**(栈:h11_impl.send → logging.emit →
    flush),整个事件循环随之卡死——不再 accept 新连接、既有连接停在 CLOSE-WAIT。
    这是经典的"管道缓冲写满死锁",纯测试基座缺陷(生产走 systemd/journald 会
    持续排空管道,不触发)。修复:起进程即挂一个后台线程逐行排空 stdout 到
    有界缓冲,`terminate_and_collect`/`collected_output` 再从缓冲取诊断 tail。"""
    sink: collections.deque = collections.deque(maxlen=_STDOUT_SINK_MAXLEN)

    def _drain() -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                sink.append(line)
        except (ValueError, OSError):  # 管道在关闭竞态下被合上,忽略
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_drain, name="everos-stub-stdout-drainer", daemon=True)
    setattr(proc, _STDOUT_SINK_ATTR, sink)
    setattr(proc, _STDOUT_DRAINER_ATTR, t)
    t.start()


def collected_output(proc: subprocess.Popen, *, join_timeout: float = 5.0) -> str:
    """取排空线程已收集的 stdout 全文(先 join 到 EOF 保证进程已产出的行都收齐)。
    进程仍在跑时也可调用(取当前快照),但典型用法是进程已退出后取诊断 tail。"""
    drainer = getattr(proc, _STDOUT_DRAINER_ATTR, None)
    if drainer is not None:
        drainer.join(timeout=join_timeout)
    sink = getattr(proc, _STDOUT_SINK_ATTR, None)
    return "".join(sink) if sink is not None else ""


def spawn_server(env: dict, *, cwd: Path = REPO_ROOT) -> subprocess.Popen:
    """起真实 `uv run --frozen --group mcp-shadow python -m everos_mcp.server`
    子进程——与生产 unit 的 `ExecStart` 逐字段一致的启动方式(仅 cwd 换成本
    worktree 根、env 换成隔离 env)。

    `start_new_session=True`(新会话/进程组,pgid==pid):`uv run` 会 fork 出真正
    跑 `python -m everos_mcp.server` 的子进程再转发信号——SIGTERM 时 `uv` 能
    捕获并转发(验证过:能观察到 uvicorn graceful shutdown、最终 returncode
    143),但 SIGKILL 谁都捕获不了,内核直接干掉 `uv` 本身,它的子进程(真正
    持有 ledger flock 的那个)会变成孤儿继续存活、继续攥着锁——kill -9 类
    故障案例必须用 `os.killpg` 打整个进程组,不能只 `send_signal` 给 `proc.pid`
    (那只是 `uv` 自己的 PID)。

    子进程 stdout 挂后台排空线程(见 `_attach_stdout_drainer`)——不排空会因
    uvicorn 每请求同步写 access log 撑满管道缓冲、卡死事件循环(Task 9 定位)。"""
    proc = subprocess.Popen(
        ["uv", "run", "--frozen", "--group", "mcp-shadow", "python", "-m", "everos_mcp.server"],
        cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    _attach_stdout_drainer(proc)
    return proc


def killpg_hard(proc: subprocess.Popen) -> None:
    """SIGKILL 整个进程组(见 `spawn_server` 文档字符串——`uv run` 的真实子
    进程不会响应只发给 `proc.pid` 的 SIGKILL 转发,必须打组)。"""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def _try_call(url: str, token: str, task: str, limit: int = 3):
    client = Client(url, auth=token)
    async with client:
        return await client.call_tool("everos_search", {"task": task, "limit": limit})


def wait_ready(port: int, token: str, *, timeout: float = 60.0, proc: Optional[subprocess.Popen] = None):
    """轮询直到子进程真正完成协议握手并成功应答一次 `everos_search`(不是端口
    能连就算就绪——bootstrap 序还没走完之前端口根本没监听,uvicorn 起来后到
    `everos_search` 能拿到正常响应之间也可能有 pin 采集等待窗口)。返回该次
    调用的结果;`proc` 提供时,若子进程提前退出则立即报错而不是傻等超时。"""
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"server 子进程在就绪前已退出,rc={proc.returncode}")
        try:
            return asyncio.run(_try_call(url, token, "warmup 就绪探测查询"))
        except Exception as e:  # noqa: BLE001 —— 就绪轮询期间的连接/握手失败都重试
            last_err = e
            time.sleep(0.3)
    raise RuntimeError(f"server 在 {timeout}s 内未就绪: {last_err}")


def wait_tcp_ready(port: int, *, timeout: float = 30.0, proc: Optional[subprocess.Popen] = None) -> None:
    """只等端口能连上(不发协议层请求)——`SHADOW_FAULT=ops_write_fail` 场景下
    第一次真实工具调用会让服务端进程自杀(`os._exit(86)`),不能用
    `wait_ready()`(它自己就是一次会触发致命故障的工具调用)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"server 子进程在端口就绪前已退出,rc={proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"端口 {port} 在 {timeout}s 内未开始监听")


def terminate_and_collect(proc: subprocess.Popen, *, timeout: float = 10.0) -> tuple[int, str]:
    """SIGTERM(`uv run` 正常会转发给真正的 server 子进程、等它 graceful
    shutdown 完再退出——实测 returncode==143),超时则整组 SIGKILL(`proc.kill()`
    只杀得到 `uv` 自己,见 `killpg_hard` 文档字符串),收集
    (returncode, 排空线程已捕获的 stdout/stderr tail)。

    stdout 由 `spawn_server` 挂的后台排空线程持续读取(不能改回 `communicate()`
    ——那样只在退出时读一次,运行期间管道会被 uvicorn 的 per-request access log
    写满而卡死事件循环,见 `_attach_stdout_drainer`)。"""
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        killpg_hard(proc)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
    return proc.returncode, collected_output(proc, join_timeout=timeout)
