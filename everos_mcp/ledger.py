# everos_mcp/ledger.py
"""三条流(ops/accepted/scored)持久层 + 崩溃协议 + flock。

规则(见任务简报 Task 4,均为审查阻断项):
- 每条流一个后台单线程 writer(`LedgerWriter`):`submit()` 阻塞等回执,回执
  在 `os.fsync` **之后**才发出;等待超时 -> `LedgerTimeout`,但该行**仍在队列
  里**,writer 恢复后照写(late-commit,调用方决定要不要另外记 response_aborted
  / mark_abort)。真正的写入失败(含故障注入)与"调用方等不及"是两种不同
  的失败模式:前者该行不会再写(`LedgerUnavailable`),后者行迟早会写。
- scored 流的两项锁死职责在 writer 线程内完成,不在提交方:
  ① `attempt_no` 由 writer 按 rid 串行分配(内存计数器,启动时从磁盘现存
     scored 行的 max 恢复,跨重启/跨 producer 并发提交都不会重号);
  ② 健康谓词 `validator(row, accepted_row) -> bool` 在写入时机调用(就是
     Task 6 `healthy` 本尊,零包装直接注入);对 status=="ok" 的行调用,
     返回 False -> **writer 自己**改写为 status="retryable_error" +
     score_error_code="health_predicate_reject" 再落盘。任何 producer(实时
     打分 / reconciliation / manual rescore)都绕不过这一步。
- `aborts.log` 由 `Ledger.mark_abort(rid)` 以 `O_APPEND|O_CREAT` 直写一行
  `{rid,ts}` + fsync,**完全不经过任何 writer 线程/队列**——writer 挂死时
  仍必须能写(O_APPEND 小行在 Linux 上原子)。物化/effective_status 判定时
  aborts.log 内的 rid 一律 `effective_status=="error"`,优先级最高。
- 启动协议(顺序冻结):
  ① 对 `root/.lock` 取 `flock` 排他锁,拿不到 -> raise `LedgerLocked`;
  ② 残尾检测:ops/accepted/scored 三个 jsonl 里任一文件尾部不是完整行(说明
     上次崩在这一行的 fsync 之前)-> 整份文件 rename 为
     `<name>.sealed-<ts>-<uuid4hex8>.jsonl`(时间戳+uuid 前 8 位保证同秒重启
     也不会覆盖)+ 父目录 fsync,再新开一个空的当前段文件,**新段文件创建
     后同样父目录 fsync**(两处 fsync 都是为了让"重命名"和"新文件出现"这两
     件事本身在下一次崩溃前是持久的);
  ③ chmod 700/600 校验:已存在的目录/文件权限必须精确等于期望值,不符
     -> raise `LedgerPermissionError`,**绝不静默 chmod 修复**——账本里可能有
     查询明文,权限被改过是篡改/误配置信号,fail-stop 优先于自愈(与
     config.py 对 `ledger_dir` 的态度一致)。只有本 Ledger 自己新建的目录/
     文件才会被赋予正确权限(root 目录 0700,四个账文件 0600)。
- 段读取:`iter_rows(root, stream_name)` 按段序读——sealed-* 段(按文件名
  排序,时间戳前缀保证顺序)在前,当前段在后;半行(JSON 解析失败的残行,
  典型是被 sealed 的段自己的最后一行)跳过不计入返回行,只计入告警数,
  返回 `(rows, warning_count)`。
- 行构造器全是纯函数,`schema_version=1`,不接触磁盘。`accepted_row` 的
  判别联合表(spec §3)按 stage 精确定义 required/optional/forbidden/fixed
  四类字段;对"必须缺席"的字段传入任何非缺席值(包括显式 None)一律 raise
  ——不允许拿 null 冒充"没有这个字段"。
- `effective_status(ops_rows, accepted_events, abort_rids, rid)` 是**唯一**
  规范实现:物化 / reconciliation / orphan 判定必须复用它,不允许各自重新
  实现优先级判断。
- 账目录下全体文件 0600、子目录 0700。
"""
from __future__ import annotations

import fcntl
import json
import os
import queue
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_DIR_MODE = 0o700
_FILE_MODE = 0o600

_STREAM_NAMES = ("ops", "accepted", "scored")

# 判别联合字段全集(rid/ts/traffic_class 是每条 accepted 行都有的基础信封字段,
# 不参与本表——见 accepted_row 里的 `out` 初始化)。
_UNION_FIELDS = (
    "query", "q_len", "everos_rid", "candidates", "returned_ids",
    "constructed_decision", "error_code", "search_ms", "pre_commit_ms",
    "config_fp", "error_detail",
)

_ABSENT = object()  # 哨兵:与"显式传 None"严格区分——传 None 也算"传了值"。


# ======================================================================
# 异常
# ======================================================================

class LedgerLocked(Exception):
    """`root/.lock` 已被另一实例持有 flock——本进程不是唯一 writer,拒绝启动。"""


class LedgerTimeout(Exception):
    """`submit`/`submit_scored` 等回执超时。该行仍在 writer 队列里,writer
    恢复后仍会照写(late-commit)——调用方不应认为这行"没写",只应认为
    "这次没等到确认"。"""


class LedgerUnavailable(Exception):
    """写入本身失败(含故障注入 `fault=`),回执已到但不是超时——该行不会
    再被写入。调用方(server.py)据此映射为 error_code=ledger_unavailable
    之类的对外语义。"""


class LedgerPermissionError(Exception):
    """账目录下已存在的目录/文件权限不符预期(目录非 0700 / 文件非 0600)。

    这是 fail-stop 信号,不是"顺手修一下"的机会:账本里可能有查询明文,权限
    被谁改过是篡改/误配置的征兆,与 config.py 对 `ledger_dir` 的态度一致——
    宁可拒绝启动,也不要在没人看见的情况下悄悄把可疑状态"修好"。只有 Ledger
    自己新建的目录/文件才会被赋予正确权限;已经存在的一律校验、不符直接
    raise,绝不静默 chmod 覆盖。"""


# ======================================================================
# 小工具
# ======================================================================

def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify_mode(path: Path, expected_mode: int) -> None:
    """已存在的目录/文件权限必须精确等于 `expected_mode`,否则 raise
    `LedgerPermissionError`——不修复,只拒绝。"""
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual != expected_mode:
        kind = "目录" if path.is_dir() else "文件"
        raise LedgerPermissionError(
            f"{path} 是{kind},权限应为 {oct(expected_mode)},实际 {oct(actual)}"
            "——拒绝自动修复(疑似篡改或误配置),请人工核实后手动 chmod。"
        )


def _ensure_file_mode(path: Path, mode: int = _FILE_MODE) -> None:
    """文件不存在 -> 由本 Ledger 新建并赋权(新建的东西当然要给对权限,这部分
    不变);已存在 -> 只校验,不符直接 raise,绝不静默 chmod 覆盖(见
    `LedgerPermissionError`)。不截断已有内容。"""
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, mode)
        os.close(fd)
        os.chmod(path, mode)  # umask 可能已冲掉 os.open 的 mode,显式再钉一次
        _fsync_dir(path.parent)
    else:
        _verify_mode(path, mode)


def _ensure_dir_mode(path: Path, mode: int = _DIR_MODE) -> None:
    """目录版的 `_ensure_file_mode`:不存在 -> 新建并赋权;已存在 -> 只校验,
    不符直接 raise。"""
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
    else:
        _verify_mode(path, mode)


# ======================================================================
# 行构造器(纯函数,schema_version=1)
# ======================================================================

def ops_started(rid: str, traffic_class: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "started",
        "rid": rid,
        "ts": time.time(),
        "traffic_class": traffic_class,
    }


def ops_terminal(rid: str, effective_status: str, error_code: str | None = None) -> dict:
    """`effective_status` 是即将返回给调用方的最终状态(hit/abstain_empty/
    error)。`effective_status=="error"` 时必须给 `error_code`——不允许记一条
    "错误但不知道错在哪"的终态行。"""
    if effective_status == "error" and error_code is None:
        raise ValueError("ops_terminal: effective_status=='error' 必须带 error_code")
    row = {
        "schema_version": 1,
        "kind": "terminal",
        "rid": rid,
        "ts": time.time(),
        "effective_status": effective_status,
    }
    if error_code is not None:
        row["error_code"] = error_code
    return row


def response_aborted_row(rid: str, reason: str) -> dict:
    """追加进 accepted 流:accepted 回执超时之后的 best-effort 记账,标记"这次
    响应我方已经放弃等待"。effective_status() 把它当最高优先级之一的 error。"""
    return {
        "schema_version": 1,
        "kind": "response_aborted",
        "rid": rid,
        "ts": time.time(),
        "reason": reason,
    }


# stage -> {fixed: {field: value}, required: (...), optional: (...), forbidden: (...)}
# 四个桶互斥覆盖 `_UNION_FIELDS` 全集(见模块末尾的自检),没有遗漏字段。
_STAGE_FIELDS: dict[str, dict[str, Any]] = {
    "contract_reject": {
        "fixed": {"query": None, "constructed_decision": "error"},
        "required": ("error_code", "pre_commit_ms", "config_fp"),
        "optional": ("error_detail",),
        "forbidden": ("candidates", "everos_rid", "search_ms", "returned_ids", "q_len"),
    },
    "gated": {
        "fixed": {"constructed_decision": "error", "error_code": "review_overdue"},
        "required": ("query", "q_len", "pre_commit_ms", "config_fp"),
        "optional": ("error_detail",),
        "forbidden": ("candidates", "everos_rid", "search_ms", "returned_ids"),
    },
    "upstream_fail": {
        "fixed": {"constructed_decision": "error"},
        "required": ("query", "q_len", "error_code", "pre_commit_ms", "config_fp"),
        "optional": ("error_detail",),
        "forbidden": ("candidates", "everos_rid", "search_ms", "returned_ids"),
    },
    "empty": {
        "fixed": {"constructed_decision": "abstain_empty", "candidates": [], "returned_ids": []},
        "required": ("query", "q_len", "everos_rid", "search_ms", "pre_commit_ms", "config_fp"),
        "optional": ("error_code", "error_detail"),
        "forbidden": (),
    },
    "hit": {
        "fixed": {"constructed_decision": "hit"},
        "required": ("query", "q_len", "everos_rid", "search_ms", "candidates",
                     "returned_ids", "pre_commit_ms", "config_fp"),
        "optional": ("error_code", "error_detail"),
        "forbidden": (),
    },
}


def accepted_row(
    stage: str,
    rid: str,
    ts: float,
    traffic_class: str,
    *,
    query: Any = _ABSENT,
    q_len: Any = _ABSENT,
    everos_rid: Any = _ABSENT,
    candidates: Any = _ABSENT,
    returned_ids: Any = _ABSENT,
    constructed_decision: Any = _ABSENT,
    error_code: Any = _ABSENT,
    search_ms: Any = _ABSENT,
    pre_commit_ms: Any = _ABSENT,
    config_fp: Any = _ABSENT,
    error_detail: Any = _ABSENT,
) -> dict:
    """按 `stage` 断言判别联合字段(spec §3 精确表)。

    stage ∈ {contract_reject, gated, upstream_fail, empty, hit}。每个字段落
    在四类桶之一:
    - fixed:该 stage 下值被固定(如 gated 的 error_code 恒为 review_overdue,
      contract_reject 的 query 恒为 None)。调用方可以不传(自动填入固定值),
      也可以传一致的值;传了不一致的值 -> raise。
    - required:调用方必须提供(值任意),不提供 -> raise。
    - optional:调用方可提供可不提供,提供了就写入,没提供就整个 key 缺席。
    - forbidden:该字段对这个 stage "必须缺席"——调用方传入任何非 `_ABSENT`
      的值(包括显式 None)都 raise,不允许拿 null 占位假装"有这个字段"。
    """
    spec = _STAGE_FIELDS.get(stage)
    if spec is None:
        raise ValueError(f"accepted_row: 未知 stage {stage!r}")

    supplied = {
        "query": query,
        "q_len": q_len,
        "everos_rid": everos_rid,
        "candidates": candidates,
        "returned_ids": returned_ids,
        "constructed_decision": constructed_decision,
        "error_code": error_code,
        "search_ms": search_ms,
        "pre_commit_ms": pre_commit_ms,
        "config_fp": config_fp,
        "error_detail": error_detail,
    }

    out: dict = {
        "schema_version": 1,
        "kind": "accepted",
        "stage": stage,
        "rid": rid,
        "ts": ts,
        "traffic_class": traffic_class,
    }

    fixed = spec["fixed"]
    required = spec["required"]
    optional = spec["optional"]
    forbidden = spec["forbidden"]

    for name in _UNION_FIELDS:
        value = supplied[name]
        if name in forbidden:
            if value is not _ABSENT:
                raise ValueError(
                    f"accepted_row(stage={stage!r}): 字段 {name!r} 必须缺席,"
                    f"不接受任何值(含 None),实际传入 {value!r}"
                )
            continue
        if name in fixed:
            fixed_value = fixed[name]
            if value is _ABSENT:
                out[name] = fixed_value
            elif value != fixed_value:
                raise ValueError(
                    f"accepted_row(stage={stage!r}): 字段 {name!r} 固定为 "
                    f"{fixed_value!r},传入不一致的值 {value!r}"
                )
            else:
                out[name] = value
            continue
        if name in required:
            if value is _ABSENT:
                raise ValueError(f"accepted_row(stage={stage!r}): 缺少必需字段 {name!r}")
            out[name] = value
            continue
        if name in optional:
            if value is not _ABSENT:
                out[name] = value
            continue
        # 理论上不可达:_STAGE_FIELDS 的四类桶覆盖 _UNION_FIELDS 全集(见模块
        # 末尾自检),留一道防线避免以后加字段却忘了分类导致悄悄放行。
        raise AssertionError(f"accepted_row: 字段 {name!r} 未在 stage={stage!r} 的桶分类中")

    return out


def scored_row(
    rid: str,
    producer: str,
    status: str,
    per_card: dict,
    pins: dict,
    score_error_code: str | None = None,
    score_error_detail: str | None = None,
    lib_counts: dict | None = None,
    count_ts: float | None = None,
) -> dict:
    """不含 `attempt_no`/`written_ts`——由 scored-writer 在写入时机填入。

    `lib_counts` 缺席不打死健康(漂移归因辅助字段,非复现 pin);缺席时应该
    在 `score_error_detail` 里说明原因(调用方职责,本函数只如实记录传入的
    值,不代为编造)。"""
    row: dict = {
        "schema_version": 1,
        "kind": "scored",
        "rid": rid,
        "producer": producer,
        "status": status,
        "per_card": per_card,
        "pins": pins,
    }
    if score_error_code is not None:
        row["score_error_code"] = score_error_code
    if score_error_detail is not None:
        row["score_error_detail"] = score_error_detail
    if lib_counts is not None:
        row["lib_counts"] = lib_counts
    if count_ts is not None:
        row["count_ts"] = count_ts
    return row


# ======================================================================
# effective_status —— canonical 单一实现
# ======================================================================

_VALID_TERMINAL_STATUS = ("hit", "abstain_empty", "error")


def effective_status(
    ops_rows: list[dict],
    accepted_events: list[dict],
    abort_rids: set[str],
    rid: str,
) -> str:
    """物化 / reconciliation / orphan 判定的唯一规范实现,禁止各算各的。

    优先级(从高到低):
    1. `rid` 出现在 `abort_rids`(aborts.log 全集)-> "error"。这是最高优先级
       ——重启后 reconciliation 才不会把已经 abort 掉的 rid 当命中补分。
    2. `accepted_events` 里该 rid 有一条 `kind=="response_aborted"` -> "error"。
    3. ops 侧:没有 started 行 / 有 started 没有 terminal / terminal 结构损坏
       (缺 `effective_status` 或值不在 hit/abstain_empty/error 里)/ 同 rid
       出现多条 terminal(late-commit 冲突)-> "error"。
    4. 否则取(唯一的)terminal 行的 `effective_status` 值。
    """
    if rid in abort_rids:
        return "error"

    for ev in accepted_events:
        if ev.get("rid") == rid and ev.get("kind") == "response_aborted":
            return "error"

    started = [r for r in ops_rows if r.get("rid") == rid and r.get("kind") == "started"]
    if not started:
        return "error"

    terminals = [r for r in ops_rows if r.get("rid") == rid and r.get("kind") == "terminal"]
    if not terminals:
        return "error"
    if len(terminals) > 1:
        return "error"

    status = terminals[0].get("effective_status")
    if status not in _VALID_TERMINAL_STATUS:
        return "error"
    return status


# ======================================================================
# 段读取 + aborts.log 读取(standalone,不需要持有 flock 的 Ledger 实例——
# materialize.py/reconciliation 只读扫描时用这两个函数,不必抢占 root/.lock)
# ======================================================================

def _segment_paths(root: Path, stream_name: str) -> list[Path]:
    root = Path(root)
    sealed = sorted(root.glob(f"{stream_name}.sealed-*.jsonl"))
    paths = list(sealed)
    current = root / f"{stream_name}.jsonl"
    if current.exists():
        paths.append(current)
    return paths


def iter_rows(root: Path, stream_name: str) -> tuple[list[dict], int]:
    """按段序(sealed-* 在前,当前段在后)读一条流的全部行。半行(JSON 解析
    失败的残行——典型是被 sealed 的段自身最后一行,崩溃时写到一半)跳过、
    不计入返回的行,只计入告警数。返回 `(rows, warning_count)`。"""
    rows: list[dict] = []
    warnings = 0
    for path in _segment_paths(Path(root), stream_name):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    warnings += 1
    return rows, warnings


def read_abort_rids(root: Path) -> set[str]:
    """读 `aborts.log` 全集。整行 JSON,半行跳过(与主账同容忍)。"""
    path = Path(root) / "aborts.log"
    rids: set[str] = set()
    if not path.exists():
        return rids
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = obj.get("rid")
            if rid:
                rids.add(rid)
    return rids


# ======================================================================
# LedgerWriter —— 单条流的后台单线程 writer
# ======================================================================

_SENTINEL = object()


@dataclass
class _QueueItem:
    row: dict
    accepted_row: dict | None
    is_scored: bool
    event: threading.Event
    error: Exception | None = field(default=None)


class LedgerWriter:
    """一条 append-only jsonl 流的后台单线程 writer。

    scored 流(经 `submit_scored` 提交的队列项)在写入时机额外做两件事:
    ① 按 rid 串行分配 `attempt_no`(内存计数器,`existing_rows` 用于启动时从
       磁盘现存行恢复 max);② 若配置了 `validator`,对 status=="ok" 的行调用
       `validator(row, accepted_row) -> bool`,False 则本 writer 自己把行改写
       为 retryable_error + score_error_code="health_predicate_reject"。
    `fault_reason` 非 None 时,每次写入前无条件 raise `LedgerUnavailable`——
    用于故障注入套件(生产不设即 None,零开销:多一次 `if` 判断可忽略)。
    """

    def __init__(
        self,
        path: Path,
        name: str,
        validator: Callable[[dict, dict], bool] | None = None,
        fault_reason: str | None = None,
        existing_rows: list[dict] | None = None,
    ):
        self.path = Path(path)
        self.name = name
        self.validator = validator
        self._fault_reason = fault_reason
        self._queue: queue.Queue = queue.Queue()

        self._attempt_no: dict[str, int] = {}
        for row in existing_rows or ():
            rid = row.get("rid")
            attempt = row.get("attempt_no")
            if rid is None or attempt is None:
                continue
            self._attempt_no[rid] = max(self._attempt_no.get(rid, 0), attempt + 1)

        _ensure_file_mode(self.path)
        self._fh = open(self.path, "a", encoding="utf-8")
        self._thread = threading.Thread(
            target=self._run, name=f"ledger-writer-{name}", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------
    def submit(self, row: dict, timeout: float = 5.0) -> None:
        """ops/accepted 流用这个:排队一个普通行,阻塞等回执(fsync 之后才发
        出)。超时 -> `LedgerTimeout`,该行仍在队列里,writer 恢复后照写。"""
        item = _QueueItem(row=row, accepted_row=None, is_scored=False, event=threading.Event())
        self._queue.put(item)
        if not item.event.wait(timeout):
            raise LedgerTimeout(
                f"{self.name} writer: 回执超时(rid={row.get('rid')!r})——"
                "行仍在队列里,writer 恢复后仍会写入(late-commit)"
            )
        if item.error is not None:
            raise item.error

    def submit_nowait(self, row: dict) -> None:
        """P2(R4 阻断项 #7):fire-and-forget 提交——排队后立即返回,不等待任何
        回执(既不阻塞,也不确认落盘)。仍然走同一条队列,FIFO 顺序与
        `submit()`/`submit_scored()` 提交的行完全一致,只是调用方放弃了"等
        到底写没写成功"这件事。

        专用场景:late-commit 的补偿性写入(response_aborted 追加行、ops
        terminal 的补偿写)——这类写入发生时,响应结果已经由更早的一次
        `LedgerTimeout`/`LedgerUnavailable` 确定下来了,`mark_abort` 才是那
        一刻真正权威、且同步落盘的信号;继续阻塞等这类补偿写的回执只会让
        worst-case handler 延迟从"一次 5s 超时"堆叠成"两到三次 5s 超时相加",
        对正确性没有任何增益(见 `server.py::_submit_accepted_or_ledger_error`/
        `_submit_ops_terminal_nowait` 的调用点注释)。

        错误(含故障注入)会在 writer 内部照常发生,但因为没有人在等待,
        只能静默丢失——这是 best-effort 语义的应有代价,调用方不应该也不能
        对这次提交做失败处理。"""
        item = _QueueItem(row=row, accepted_row=None, is_scored=False, event=threading.Event())
        self._queue.put(item)

    def submit_scored(self, row: dict, accepted_row: dict, timeout: float = 5.0) -> None:
        """scored 流专用:队列项携带 `accepted_row` 供 writer 线程内调用
        validator;`attempt_no`/`written_ts` 由 writer 填入,不由调用方给。"""
        item = _QueueItem(row=row, accepted_row=accepted_row, is_scored=True, event=threading.Event())
        self._queue.put(item)
        if not item.event.wait(timeout):
            raise LedgerTimeout(
                f"{self.name} writer: 回执超时(rid={row.get('rid')!r})——"
                "行仍在队列里,writer 恢复后仍会写入(late-commit)"
            )
        if item.error is not None:
            raise item.error

    def alive(self) -> bool:
        return self._thread.is_alive()

    def close(self, drain: bool = True) -> None:
        """`drain=True`:等队列里已有的行全部处理完再关文件(正常关停用)。
        `drain=False`:不等待线程 join——daemon 线程会随进程退出,主要给测试
        做快速/强制收尾用,不保证队列积压已落盘。两种情况都要关文件句柄,
        否则 `drain=False` 会泄漏 `self._fh`。"""
        self._queue.put(_SENTINEL)
        if drain:
            self._thread.join(timeout=10)
        self._fh.close()

    # ------------------------------------------------------------
    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                break
            try:
                row = item.row
                if item.is_scored:
                    row = dict(row)
                    rid = row["rid"]
                    attempt_no = self._attempt_no.get(rid, 0)
                    self._attempt_no[rid] = attempt_no + 1
                    row["attempt_no"] = attempt_no
                    row["written_ts"] = time.time()
                    if (
                        self.validator is not None
                        and row.get("status") == "ok"
                        and not self.validator(row, item.accepted_row)
                    ):
                        row["status"] = "retryable_error"
                        row["score_error_code"] = "health_predicate_reject"
                if self._fault_reason is not None:
                    raise LedgerUnavailable(
                        f"{self.name} writer: 故障注入({self._fault_reason})"
                    )
                line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                self._fh.write(line)
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except Exception as e:  # noqa: BLE001 —— 回执必须带上真实异常,不能吞
                item.error = e
            finally:
                item.event.set()


# ======================================================================
# Ledger —— 三条流 + aborts.log + 启动协议
# ======================================================================

class Ledger:
    """组合三条流 writer(ops/accepted/scored)+ aborts.log + 启动崩溃协议。"""

    def __init__(self, root: Path, fault: str | None = None, scored_validator=None):
        self.root = Path(root)
        _ensure_dir_mode(self.root, _DIR_MODE)  # 新建 -> 赋权;已存在 -> 校验,不符 raise

        self._lock_path = self.root / ".lock"
        self._lock_fd = self._acquire_lock()
        try:
            self._seal_torn_tails()
            self._verify_permissions()

            scored_existing, _ = iter_rows(self.root, "scored")

            self.ops = LedgerWriter(
                self.root / "ops.jsonl", "ops",
                fault_reason="ops_write_fail" if fault == "ops_write_fail" else None,
            )
            self.accepted = LedgerWriter(
                self.root / "accepted.jsonl", "accepted",
                fault_reason="accepted_write_fail" if fault == "accepted_write_fail" else None,
            )
            self.scored = LedgerWriter(
                self.root / "scored.jsonl", "scored",
                validator=scored_validator,
                existing_rows=scored_existing,
            )

            self._aborts_path = self.root / "aborts.log"
            _ensure_file_mode(self._aborts_path)
        except BaseException:
            # 拿到 flock 之后、构造完成之前的任何失败(含权限校验 raise)都必须
            # 放锁 —— 否则调用方修好问题后重开同一个 root 会被自己这次失败的
            # 残留 flock 误挡成 LedgerLocked。
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
            raise

    # ------------------------------------------------------------
    def _acquire_lock(self) -> int:
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, _FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            raise LedgerLocked(
                f"root {self.root} 的 .lock 已被另一实例持有 flock: {e}"
            ) from None
        return fd

    def _seal_torn_tails(self) -> None:
        for name in _STREAM_NAMES:
            path = self.root / f"{name}.jsonl"
            if not path.exists():
                continue
            if path.stat().st_size == 0:
                continue
            # 只看尾部一个字节判断"最后一行是否完整",不把整份(可能很大的)
            # jsonl 读进内存——残尾检测只关心最后一次写有没有写完整。
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                last_byte = f.read(1)
            if last_byte == b"\n":
                continue
            self._seal_segment(path)

    def _seal_segment(self, path: Path) -> None:
        ts = int(time.time())
        suffix = uuid.uuid4().hex[:8]
        sealed_path = path.parent / f"{path.stem}.sealed-{ts}-{suffix}.jsonl"
        os.rename(path, sealed_path)
        _fsync_dir(path.parent)
        # 新开一个空的当前段——新段文件创建后同样父目录 fsync(spec 两处都锁死)。
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, _FILE_MODE)
        os.close(fd)
        os.chmod(path, _FILE_MODE)  # umask 可能已冲掉 os.open 的 mode,显式再钉一次(M4.2:
        # 与 blobstore.put / checkpoint 的写协议同一纪律,防病态 umask 下自我
        # DoS——_verify_permissions 后续重启会校验这份权限,不符直接拒启)
        _fsync_dir(path.parent)

    def _verify_permissions(self) -> None:
        """启动协议③:校验(不修复)。root 目录与 `.lock` 此刻必然已经存在
        (要么是本次新建、已经被赋予正确权限,要么是既有的、必须严丝合缝等于
        期望权限),用 `_verify_mode` 直接比对。三个 jsonl 流文件可能是第一次
        运行还不存在(交给下面构造 `LedgerWriter` 时新建赋权),也可能是
        `_seal_torn_tails` 刚新开的空段(已经是正确权限),也可能是正常重启后
        既有的旧文件(必须校验)——三种情况统一交给 `_ensure_file_mode` 处理。
        任一权限不符 -> `LedgerPermissionError`,不静默 chmod 覆盖。"""
        _verify_mode(self.root, _DIR_MODE)
        _verify_mode(self._lock_path, _FILE_MODE)
        for name in _STREAM_NAMES:
            _ensure_file_mode(self.root / f"{name}.jsonl")

    # ------------------------------------------------------------
    def submit_scored(self, row: dict, accepted_row: dict, timeout: float = 5.0) -> None:
        self.scored.submit_scored(row, accepted_row, timeout=timeout)

    def mark_abort(self, rid: str) -> None:
        """直写 `aborts.log`,完全不经过任何 writer 线程/队列——writer 挂死
        时仍必须能写。`O_APPEND|O_CREAT` + 单行 JSON 在 Linux 上是原子的
        (远小于 PIPE_BUF)。写失败原样上抛——此时磁盘大概率整体不可写,
        fail-stop 在即,不在这里吞掉。"""
        row = {"rid": rid, "ts": time.time()}
        line = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(self._aborts_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, _FILE_MODE)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def abort_rids(self) -> set[str]:
        return read_abort_rids(self.root)

    def iter_rows(self, stream_name: str) -> tuple[list[dict], int]:
        return iter_rows(self.root, stream_name)

    def close(self, drain: bool = True) -> None:
        self.ops.close(drain)
        self.accepted.close(drain)
        self.scored.close(drain)
        # 释放 flock 与"是否等 writer 排空"无关——就算 drain=False(不等排空),
        # 也必须放锁,否则下一次 Ledger(root) 会被自己刚关掉的这个实例的残留
        # 锁挡住(flock 只在进程退出时才会被内核自动释放)。
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ----------------------------------------------------------------------
# 模块自检:_STAGE_FIELDS 的四类桶必须无遗漏地覆盖 _UNION_FIELDS 全集,且互
# 斥(同一字段不能同时出现在两个桶里)。发现问题就在 import 时炸,而不是留到
# 某个 stage 第一次被调用才发现分类漏了字段。
for _stage, _spec in _STAGE_FIELDS.items():
    _buckets = (set(_spec["fixed"]), set(_spec["required"]), set(_spec["optional"]), set(_spec["forbidden"]))
    _union = set().union(*_buckets)
    if _union != set(_UNION_FIELDS):
        missing = set(_UNION_FIELDS) - _union
        extra = _union - set(_UNION_FIELDS)
        raise AssertionError(
            f"ledger._STAGE_FIELDS[{_stage!r}] 字段桶覆盖不全: missing={missing} extra={extra}"
        )
    overlap = set()
    for a in range(len(_buckets)):
        for b in range(a + 1, len(_buckets)):
            overlap |= _buckets[a] & _buckets[b]
    if overlap:
        raise AssertionError(f"ledger._STAGE_FIELDS[{_stage!r}] 字段桶重叠: {overlap}")
del _stage, _spec, _buckets, _union
