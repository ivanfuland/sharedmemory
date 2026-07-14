"""EverOS 生产喂料薄入口(F-E1b worker shell-out;spec §3 feeder 单元,R8 定稿)。

契约(stdout 最后一行、也是唯一一行 JSON,五值 schema 写死,codex R6-1):
    {"status": "completed|skipped|stale|no_side_effect_error|error",
     "case_entry_ids": [...], "payload_max_created_at": <ms|null>, "detail": "..."}
worker 处置由 status 字段驱动,不解析 stderr 猜。所有可预期路径 exit 0;
只有自身未捕获崩溃才非零退出(worker 按可能已触达 /add 处置:保持 running 归 nightly)。

once-only 铁律(spec §7):/add 非重放幂等——任何路径都不自动重放 /add。
退避重试(15/30/45s,M1b 口径)仅限「首个 /add 成功之前」的瞬时失败(codex R7),
判据 = 本进程对 /memory/add 的成功响应计数为 0 且异常属零副作用类。
所有拓扑经私有 env 注入(PUBLIC 仓零字面量)。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

import httpx

import everos_adapter.feed as adapter_feed
from everos_adapter.cap import make_clamper
from everos_adapter.pipeline import run_session
from everos_adapter.scan_terminal import collect_case_entry_ids

AGENT_ID = os.environ.get("EVEROS_FEED_AGENT_ID", "everos-prod")
USER_SENDER = os.environ.get("EVEROS_FEED_USER_SENDER", "user")

_ROW_SQL = (
    "SELECT idx, role, content, created_at, extra_bin, extra_json "
    "FROM messages WHERE conversation_id = ? ORDER BY idx ASC"
)


class _AddCountingHttpx:
    """只读观测钩子:计数成功 /memory/add 响应,行为逐字节透传。

    钩在 everos_adapter.feed 模块自己的 httpx 引用上(不碰全局 httpx;本进程单用途)。
    这是「首个 /add 成功之前」(codex R7)在不改 M1a 代码前提下的唯一观测点。
    """

    def __init__(self, real):
        self._real = real
        self.add_ok = 0

    def post(self, url, **kw):
        r = self._real.post(url, **kw)
        if url.rstrip("/").endswith("/memory/add") and r.status_code < 400:
            self.add_ok += 1
        return r

    def __getattr__(self, name):
        return getattr(self._real, name)


def _is_pre_add_transient(exc: Exception, add_ok: int) -> bool:
    """零 /add 副作用 + 瞬时,才允许退避重试(spec §7:退避仅限首个 /add 前)。

    - ConnectError/ConnectTimeout:请求根本没到达实例,该次零副作用;
    - 422(HTTPStatusError):实例收到并拒绝(M1b 实证 busy),该次零副作用;
    - 其余(ReadTimeout/5xx/...):响应缺失或语义不明,可能已产生副作用 → 不重试;
    - add_ok > 0:本会话已有 /add 落地,重放 run_session 会重复喂前缀 → 一律不重试。
    """
    if add_ok > 0:
        return False
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 422


def _is_no_side_effect(exc: Exception, add_ok: int) -> bool:
    """终态分类判据(≠退避判据):零 /add 副作用的确定失败 → no_side_effect_error 回 pending。

    覆盖 spec §5 预算拒三态:LiteLLM 硬拒经 EverOS 透出为 4xx(402/429 等)时,若发生在
    首个 /add 成功之前 = 服务端收到并拒绝 = 确定零副作用 → 回 pending,加预算后自然重试。
    5xx / ReadTimeout 语义不明(可能已部分处理)→ 不算,走 error 留 running 归 nightly。
    """
    if add_ok > 0:
        return False
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500
