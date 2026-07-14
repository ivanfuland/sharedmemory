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


def _emit(status: str, case_entry_ids=None, payload_max=None, detail: str = "") -> None:
    """打印唯一一行契约 JSON 并退出 0(schema 五值写死,codex R6-1)。"""
    print(json.dumps({
        "status": status,
        "case_entry_ids": case_entry_ids or [],
        "payload_max_created_at": payload_max,
        "detail": detail,
    }, ensure_ascii=False))
    sys.exit(0)


def _read_rows(cass_db: str, external_id: str):
    """mode=ro 读 canonical rows;返回 (rows, payload_max_created_at, conv_found)。

    冷却核对必须与读取喂料 rows 在同一只读连接内完成(codex R4-3 + R5-4:检查绑定喂料快照),
    故本函数一次连接里读完 rows 并算 max(created_at),比较留给 main。
    conv 不存在 → ([], None, False);重复 external_id → 抛 RuntimeError(F1 已隔离重复组,
    走到这里说明绕过了 F1,fail-loud;非零退出 = worker error 路径)。
    """
    con = sqlite3.connect(f"file:{cass_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        convs = con.execute(
            "SELECT id FROM conversations WHERE external_id = ?", (external_id,)
        ).fetchall()
        if len(convs) == 0:
            return [], None, False
        if len(convs) > 1:
            raise RuntimeError(f"duplicate external_id in CASS ({len(convs)} convs)")
        rows = [dict(r) for r in con.execute(_ROW_SQL, (convs[0]["id"],)).fetchall()]
    finally:
        con.close()
    payload_max = max((r["created_at"] for r in rows if r["created_at"] is not None), default=None)
    return rows, payload_max, True


def _wait_terminal(memory_root: str, session_id: str, window_s: int, poll_s: int = 5) -> list:
    """轮询 markdown 出卡即 completed(M1a 公开面 collect_case_entry_ids);超窗返回空 → skipped。

    生产不解析实例日志——M1b 的日志 regex 归因是仪器件(codex R0-5/R1-6),禁止搬进生产。
    窗口默认 360s = M1b 实测 240s 上限 + 裕量(推荐默认,试点批校准)。
    """
    deadline = time.time() + window_s
    while time.time() < deadline:
        ids = collect_case_entry_ids(memory_root, session_id)
        if ids:
            return ids
        time.sleep(poll_s)
    return collect_case_entry_ids(memory_root, session_id)


def main() -> None:
    # 等号形传参铁律:external_id 可以 -- 开头,等号形不滑批量;未知 --* argparse 自 fail-loud。
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--external-id", required=True)
    ap.add_argument("--claim-msg-ts", type=int, required=True)
    ap.add_argument("--short-sid", required=True)
    a = ap.parse_args()

    cass_db = os.environ["EVEROS_CASS_DB"]
    base_url = os.environ["EVEROS_PROD_BASE_URL"]
    memory_root = os.environ["EVEROS_PROD_MD_ROOT"]
    window_s = int(os.environ.get("EVEROS_TERMINAL_WINDOW_S", "360"))

    # ── 读 rows + 快照内冷却核对(零 /add 之前;codex R4-3/R5-4)──
    rows, payload_max, found = _read_rows(cass_db, a.external_id)
    if not rows:
        _emit("skipped", detail="adapter_skipped:no_feedable" if found else "conv_not_found_in_cass")
    if payload_max is not None and payload_max > a.claim_msg_ts:
        _emit("stale", payload_max=payload_max)  # 喂中出现新消息:零 /add,worker CAS 清租约回 pending

    # ── /add 观测钩子 + 退避(仅首个 /add 成功前;codex R7)──
    probe = _AddCountingHttpx(httpx)
    adapter_feed.httpx = probe
    result = None
    for attempt in range(4):
        try:
            # 参数与成本探针完全一致(clamper/caps 走 M1a 默认)——$0.00514/会话就是这个口径量出来的,不自行改。
            result = run_session(base_url, a.short_sid, rows, AGENT_ID, USER_SENDER,
                                 clamper=make_clamper())
            break
        except httpx.HTTPError as e:
            if _is_pre_add_transient(e, probe.add_ok) and attempt < 3:
                time.sleep(15 * (attempt + 1))  # 15/30/45s,M1b 口径
                continue
            if _is_no_side_effect(e, probe.add_ok):
                # 退避耗尽的瞬时失败 + 确定性 4xx 拒(含预算拒,spec §5)→ 零副作用,可安全回 pending
                _emit("no_side_effect_error", payload_max=payload_max,
                      detail=f"pre-add no-side-effect failure: {type(e).__name__}: {e}")
            _emit("error", payload_max=payload_max,
                  detail=f"add_ok={probe.add_ok} {type(e).__name__}: {e}")  # 可能已触达 /add → worker 保持 running

    if result.get("skipped"):
        _emit("skipped", payload_max=payload_max, detail="adapter_skipped:no_feedable")

    case_ids = _wait_terminal(memory_root, a.short_sid, window_s)
    if case_ids:
        _emit("completed", case_entry_ids=case_ids, payload_max=payload_max)
    _emit("skipped", payload_max=payload_max, detail="no_terminal_within_window")


if __name__ == "__main__":
    main()
