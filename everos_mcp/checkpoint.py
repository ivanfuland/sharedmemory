# everos_mcp/checkpoint.py
"""有界运行机制(Task 5,审查阻断项均见任务简报)。

server.py(Task 8)不得永远无监督地跑下去:上线 30 天或攒够 200 条 real
查询后进入 "due"(到点);到点后 7 天内没有记一条复审 -> "overdue",
server 一律对外返回 error `review_overdue`(gating 本身是 Task 8 的活,本
模块只产出可判读的状态机 + 落盘)。

设计要点:
- `Checkpoint(root)`:meta 文件唯一落在 `root/meta.json`。`root` 与
  ledger 的 root 是同一个目录(Task 8 启动序:Ledger 先起,已经把该目录
  钉成 0700;本模块只管 meta.json 这一个文件 0600,不重复校验/新建目录
  权限——避免与 ledger.py 的目录门禁重复判定、口径打架)。
- `init_or_load(ledger_has_rows, earliest_ledger_ts=None, now=None)`:
  无 meta 且无账行 -> 原子创建 `{launched_ts: now}`;有账行但 meta 缺失/
  损坏(JSON 解析失败或缺 launched_ts)/launched_ts 晚于最早账行 ts(时钟
  回拨或 meta 被替换成新实例的信号)-> `CheckpointCorrupt`,fail-closed
  拒启,绝不静默重建。`earliest_ledger_ts` 由调用方(server.py,持有
  Ledger)算好传入——本模块不读 ledger 文件。
- `state(real_query_count, now)`:到点判据 `now-launched_ts >= 30 天` 或
  `real_query_count >= 200`。首次判定到点时原子写入 `due_since=now`(计数
  触发的到点没有自然时间锚,不持久化就算不出宽限期起点;之后重复判定
  due_since 不再改写)。`due_since` 之后**新**记的复审(review.ts >=
  due_since)会让状态回到 "ok"——**早于** due_since 的复审(比如到点前的
  自愿复审)不算数,必须是"到点之后"的复审才能解除当前这次到点(见任务
  说明里给定的口径)。据此,一次到点只需一条"到点之后"的复审即可永久解除
  ——因为到点判据(时间/计数)本身单调只增,不会自然回落,`due_since`
  也不会自动重置,所以"复审 ts >= due_since"一旦成立就恒成立。
- `record_review(decision, by, note, now=None)`:追加一条
  `{ts, decision, by, note}` 进 `meta["reviews"]`,原子写协议 = tmp 写入+
  fsync(tmp) -> rename -> fsync(父目录)(与 blobstore.put / ledger 的段
  切换同一套协议)。`decision` 必须是 continue/calibrate/stop 之一。
- CLI:`python -m everos_mcp.checkpoint review --decision <continue|
  calibrate|stop> --by <who> --note <text>` —— overdue 恢复的生产操作
  路径。root 经 `everos_mcp.config.load().ledger_dir` 取得,不直读
  env(config.py 铁律:SHADOW_*/EVEROS_* 只能在 config.py 里读)。
- 跨进程锁(final-review 修复项,extends M5.x):server.py 用进程内
  `threading.Lock`(`_CHECKPOINT_LOCK`)串行化同进程内对 `state()` 的并发
  调用,但 CLI `review` 子命令跑在**独立进程**,不共享那把锁——首次到点的
  `due_since` 落盘(在 `state()` 内)可能与 `record_review()` 的追加写竞态
  (两者都是读-改-rename 同一份 `meta.json`)。本模块用 `root/meta.lock` 上的
  `fcntl.flock`(阻塞式 `LOCK_EX`,不设超时——单次读-改-写是毫秒级操作)包住
  三处读-改-写:`init_or_load()` 的首次原子创建、`state()` 的 due_since 首次
  持久化、`record_review()` 的追加。server 与 CLI 都走这三个方法,一份 flock
  helper 天然覆盖两侧,不需要分别在两处调用方实现。
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path

_FILE_MODE = 0o600

DUE_SECONDS = 30 * 86400
DUE_QUERY_COUNT = 200
OVERDUE_GRACE_SECONDS = 7 * 86400

VALID_DECISIONS = ("continue", "calibrate", "stop")


class CheckpointCorrupt(Exception):
    """meta.json 缺失(但账本已有行)/ 解析失败 / launched_ts 晚于最早账行 ts
    ——fail-closed 拒绝启动或拒绝操作,绝不静默重建/修复。"""


def _require_finite_ts(value, *, field: str) -> None:
    """P1g:`json.loads` 默认接受 `NaN`/`Infinity`/`-Infinity` 字面量(Python
    json 模块的非标准扩展),`isinstance(NaN, float)` 也为真——单纯的类型检查
    挡不住这类值混进时间戳字段(静默污染 due/overdue 判据的算术)。任何时间戳
    类字段必须是有限实数(非 bool——bool 是 int 子类但不当数值时间戳处理),
    否则 fail-closed。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CheckpointCorrupt(f"meta.json 字段 {field} 非法(非数值): {value!r}")
    if not math.isfinite(value):
        raise CheckpointCorrupt(f"meta.json 字段 {field} 非有限数值(NaN/Inf): {value!r}")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, obj: dict) -> None:
    """tmp 写入+fsync(tmp) -> rename -> fsync(父目录)。tmp 名唯一
    (pid+uuid),失败清理不留残 tmp。"""
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    tmp_path = path.parent / f".tmp.{os.getpid()}.{uuid.uuid4().hex}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, _FILE_MODE)  # umask 可能冲掉 os.open 的 mode,显式再钉一次
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.rename(tmp_path, path)
    _fsync_dir(path.parent)


class Checkpoint:
    """`root/meta.json` 的有界运行状态机。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.root / "meta.json"
        self.lock_path = self.root / "meta.lock"

    # ------------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        """跨进程互斥:`root/meta.lock` 上的阻塞式 `flock(LOCK_EX)`,包住一次
        完整的"读 meta -> 决定 -> 写 meta"。文件不存在则以 0600 新建;
        `os.chmod` 显式再钉一次(umask 可能冲掉 `os.open` 的 mode,与
        blobstore/ledger 的写协议同一纪律)。退出时无论是否异常都
        `LOCK_UN` + 关闭 fd——不留跨调用持有的锁。"""
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, _FILE_MODE)
        try:
            os.chmod(self.lock_path, _FILE_MODE)
            fcntl.flock(fd, fcntl.LOCK_EX)  # 阻塞式,不设超时——单次操作是毫秒级
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ------------------------------------------------------------
    def _try_load(self) -> dict | None:
        """返回解析后的 meta dict;文件不存在返回 None;存在但解析失败/缺
        `launched_ts` -> raise `CheckpointCorrupt`(不区分"损坏"与"结构不
        对",都是 fail-closed 信号)。

        P1g:`launched_ts`/`due_since`(存在时)/`reviews[*].ts`(存在时)一律
        必须是有限实数——`json.loads` 会放行 `NaN`/`Infinity` 字面量,纯类型
        检查(`isinstance(..., float)`)对 NaN 也返回 True,必须显式
        `math.isfinite` 校验,否则这类值会静默污染 due/overdue 时间算术
        (`now - NaN` 恒为 NaN,比较运算恒为 False,状态机悄悄卡死而不报错)。

        P2(R4 阻断项 #5):`obj.get("reviews") or []` 对"falsy 但非列表"的值
        (`{}`/`""`/`0`/`False`)会静默当成"没有复审记录"处理——`or []` 这个
        写法只在乎真假值,不在乎类型,`reviews` 字段本该是列表却被塞了这类
        杂质时,循环体压根不会跑,篡改/损坏被悄悄放行而不是 fail-closed。
        修法:显式区分"键缺失"(视为空列表,与 `setdefault` 行为一致)与
        "键存在但值非列表"(不论真假值,一律 `CheckpointCorrupt`)。"""
        if not self.meta_path.exists():
            return None
        try:
            raw = self.meta_path.read_text(encoding="utf-8")
            obj = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            raise CheckpointCorrupt(f"{self.meta_path} 解析失败: {e}") from e
        if not isinstance(obj, dict) or not isinstance(obj.get("launched_ts"), (int, float)):
            raise CheckpointCorrupt(f"{self.meta_path} 缺少合法 launched_ts 字段")
        _require_finite_ts(obj["launched_ts"], field="launched_ts")

        due_since = obj.get("due_since")
        if due_since is not None:
            _require_finite_ts(due_since, field="due_since")
            if due_since < obj["launched_ts"]:
                raise CheckpointCorrupt(
                    f"{self.meta_path} due_since={due_since!r} 早于 "
                    f"launched_ts={obj['launched_ts']!r}——不可能合法出现,疑似篡改/损坏"
                )

        if "reviews" in obj:
            reviews = obj["reviews"]
            if not isinstance(reviews, list):
                raise CheckpointCorrupt(
                    f"{self.meta_path} 字段 reviews 非列表(实际 {type(reviews).__name__}): {reviews!r}"
                )
        else:
            reviews = []
        # P1(复审记录是豁免-补偿的审计轨迹,provenance 必须可审计防"代拍"):
        # 每条复审记录必须同时具备 ts/decision/by/note 四个字段且类型/取值合法,
        # 缺一律 CheckpointCorrupt——不接受 `{"ts": due_since}` 这类裸字段
        # "解锁"到点,也不接受 decision 取值域外的字符串滑进账本。
        _REQUIRED_REVIEW_FIELDS = ("ts", "decision", "by", "note")
        for i, review in enumerate(reviews):
            if not isinstance(review, dict):
                raise CheckpointCorrupt(f"{self.meta_path} reviews[{i}] 非对象: {review!r}")
            missing = [f for f in _REQUIRED_REVIEW_FIELDS if f not in review]
            if missing:
                raise CheckpointCorrupt(
                    f"{self.meta_path} reviews[{i}] 缺字段 {missing}: {review!r}"
                )
            _require_finite_ts(review["ts"], field=f"reviews[{i}].ts")
            if review["decision"] not in VALID_DECISIONS:
                raise CheckpointCorrupt(
                    f"{self.meta_path} reviews[{i}].decision 非法(须 {VALID_DECISIONS} "
                    f"之一): {review['decision']!r}"
                )
            by = review["by"]
            if not isinstance(by, str) or not by.strip():
                raise CheckpointCorrupt(
                    f"{self.meta_path} reviews[{i}].by 非法(须非空字符串): {by!r}"
                )
            if not isinstance(review["note"], str):
                raise CheckpointCorrupt(
                    f"{self.meta_path} reviews[{i}].note 非法(须字符串): {review['note']!r}"
                )

        obj.setdefault("due_since", None)
        obj["reviews"] = reviews
        return obj

    def _require_loaded(self) -> dict:
        meta = self._try_load()
        if meta is None:
            raise CheckpointCorrupt(
                f"{self.meta_path} 不存在——必须先调用 init_or_load() 完成启动协议"
            )
        return meta

    # ------------------------------------------------------------
    def init_or_load(
        self,
        ledger_has_rows: bool,
        earliest_ledger_ts: float | None = None,
        now: float | None = None,
    ) -> dict:
        """无 meta 且无账行 -> 原子创建;有账行但 meta 缺失/损坏/launched_ts
        晚于最早账行 ts -> `CheckpointCorrupt`。`earliest_ledger_ts` 只在
        `ledger_has_rows=True` 时才有意义,由调用方(持有 Ledger 实例)算好
        传入——本模块不读账本文件。整个读-改-写在 `root/meta.lock` flock 下
        进行,与 CLI 进程的 `record_review()` 互斥(见类文档跨进程锁段)。"""
        now = time.time() if now is None else now
        with self._locked():
            meta = self._try_load()

            if meta is None:
                if ledger_has_rows:
                    raise CheckpointCorrupt(
                        f"{self.meta_path} 缺失,但账本已有行——fail-closed 拒启"
                        "(疑似 meta 被误删/换了新实例目录)"
                    )
                meta = {"launched_ts": now, "due_since": None, "reviews": []}
                _atomic_write_json(self.meta_path, meta)
                return meta

            if ledger_has_rows:
                if earliest_ledger_ts is None:
                    raise ValueError(
                        "init_or_load: ledger_has_rows=True 时必须提供 earliest_ledger_ts"
                        "(否则回拨校验会被静默跳过)"
                    )
                if meta["launched_ts"] > earliest_ledger_ts:
                    raise CheckpointCorrupt(
                        f"{self.meta_path} 的 launched_ts={meta['launched_ts']!r} 晚于"
                        f"最早账行 ts={earliest_ledger_ts!r}——疑似时钟回拨或 meta 被"
                        "替换,fail-closed 拒启"
                    )
            return meta

    # ------------------------------------------------------------
    def state(self, real_query_count: int, now: float | None = None) -> str:
        """`"ok" | "due" | "overdue"`。首次判定到点时原子持久化
        `due_since=now`。到点之后(ts >= due_since)记的复审让状态永久回到
        "ok"(到点之前的复审不算数,见模块文档)。

        `due_since` 一旦落盘就是粘性的(sticky):只有"ts >= due_since 的复审"
        能把状态解回 "ok"——本次调用重新算出的 `is_due` 只用于**首次**判定
        到点那一刻(把 due_since 从 None 变成一个具体时间戳)。已经持久化的
        due_since 不因为某次调用传入的 `real_query_count` 更低(计数上报非
        单调、重启抖动等)就被无视——不然就是一次静默的"already-due 又被原谅"
        ,违反 fail-closed 的合规控制意图。首次到点的 due_since 持久化在
        `root/meta.lock` flock 下进行,与 CLI 进程的 `record_review()` 互斥
        (见类文档跨进程锁段)——两者都是同一份 meta.json 的读-改-写。"""
        now = time.time() if now is None else now
        with self._locked():
            meta = self._require_loaded()

            due_since = meta.get("due_since")
            if due_since is None:
                launched_ts = meta["launched_ts"]
                is_due = (now - launched_ts >= DUE_SECONDS) or (real_query_count >= DUE_QUERY_COUNT)
                if not is_due:
                    return "ok"
                due_since = now
                meta["due_since"] = due_since
                _atomic_write_json(self.meta_path, meta)

            reviews = meta.get("reviews", [])
            reviewed_after_due = any(
                isinstance(r.get("ts"), (int, float)) and r["ts"] >= due_since for r in reviews
            )
            if reviewed_after_due:
                return "ok"

            if now - due_since >= OVERDUE_GRACE_SECONDS:
                return "overdue"
            return "due"

    # ------------------------------------------------------------
    def record_review(self, decision: str, by: str, note: str, now: float | None = None) -> dict:
        """追加一条复审记录,原子写回整份 meta。`decision` 必须是
        continue/calibrate/stop 之一——不接受任意字符串滑进账本。读-改-写在
        `root/meta.lock` flock 下进行——这是 CLI `review` 子命令(独立进程)
        与 server 进程内 `state()` 首次到点持久化互斥的关键一环(见类文档跨
        进程锁段)。"""
        if decision not in VALID_DECISIONS:
            raise ValueError(
                f"record_review: decision 必须是 {VALID_DECISIONS} 之一,实际 {decision!r}"
            )
        if not isinstance(by, str) or not by.strip():
            raise ValueError(f"record_review: by 必须是非空字符串,实际 {by!r}")
        now = time.time() if now is None else now
        with self._locked():
            meta = self._require_loaded()

            entry = {"ts": now, "decision": decision, "by": by, "note": note}
            meta.setdefault("reviews", []).append(entry)
            _atomic_write_json(self.meta_path, meta)
            return meta


# ======================================================================
# CLI:python -m everos_mcp.checkpoint review --decision ... --by ... --note ...
# ======================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m everos_mcp.checkpoint", allow_abbrev=False
    )
    sub = parser.add_subparsers(dest="command", required=True)

    review_p = sub.add_parser(
        "review", help="记录一次复审(overdue 恢复的生产操作路径)"
    )
    review_p.add_argument("--decision", required=True, choices=list(VALID_DECISIONS))
    review_p.add_argument("--by", required=True)
    review_p.add_argument("--note", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "review":
        from everos_mcp import config  # 延迟导入:CLI 专用,避免库路径无谓拉 config 依赖

        cfg = config.load()
        checkpoint = Checkpoint(cfg.ledger_dir)
        checkpoint.record_review(decision=args.decision, by=args.by, note=args.note)
        print(
            f"复审已记录: decision={args.decision} by={args.by!r} "
            f"root={cfg.ledger_dir}",
            file=sys.stderr,
        )
        return 0

    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
