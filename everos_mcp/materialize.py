# everos_mcp/materialize.py
"""物化脚本:join 三条流(ops/accepted/scored)-> 每查询一行 + DoD 指标计算。

规则(见任务简报 Task 6,均为审查阻断项;数值门槛取自
`docs/projects/shared-memory/specs/2026-07-16-everos-phase2b-shadow-design.md`
§7,简报本身只锁死 H==0 与硬可用性门两处反例,门槛具体数值是本模块对该 spec
一节的忠实落地,已在报告里如实声明是解读判断点):

- **per_card JSON 键编码**:`"{card_type}:{card_id}"` 字符串(tuple 不能作
  JSON key,accepted candidates 关联键也是 `(card_type,card_id)`)。
- `PIN_KEYS`:12 个可复现 pin 键,scorer.py(Task 7)的 `collect_pins` 产出
  键集必须与此严格一致。
- `healthy(scored_row, accepted_row) -> bool`:纯函数、零 I/O——**Task 8 会
  原样把它注入 `Ledger(scored_validator=healthy)`**,任何 producer(实时
  打分/reconciliation/manual rescore)写 scored 行时都绕不过这道谓词;签名
  与 `LedgerWriter` 的 `validator(row, accepted_row) -> bool` 精确对齐。
  判定:`status=="ok"` ∧ per_card 键集与 accepted candidates 编码键集严格相等
  (不多不少,含"两边都为空"这种边界情况也算相等)∧ per_card 内全部数值叶子
  `math.isfinite` ∧ pins 键集 ⊇ `PIN_KEYS` 且这些键的值都不是 None/"unknown"
  (pins 为空自然不满足超集,不必特判)。
- `fold(scored_rows, accepted_row) -> dict|None`:健康行中 attempt_no 最大;
  没有健康行则退到最新(attempt_no 最大)的 permanent_failure;再没有则退到
  最新的 retryable_error;三档都没有 -> None。**畸形 ok 行**(status=="ok"
  但缺卡/NaN/缺 pin)既不满足健康谓词、status 也不是
  permanent_failure/retryable_error,三档都进不去,不会被 fold 选中——这是
  实现的自然结果,不是额外特判。
- `score_eligible(effective, accepted_row) -> bool`:`effective=="hit"` 且
  accepted 行的 candidates 非空列表。`accepted_row` 为 None 时视为不可打分。
- `materialize(root, out) -> Stats`:只处理 `traffic_class=="real"` 的查询
  (traffic_class 来自 ops started 行,该行是每条查询唯一保证存在的记录,
  即使主账/scored 写失败也不受影响——这是"读分母只读 ops 流"纪律在物化侧
  的直接体现)。effective_status 判定完全复用 `ledger.effective_status`,
  不在本模块重新实现优先级逻辑。
- CLI `python -m everos_mcp.materialize <ledger_dir> <out_name>`:输出路径
  强制落在 `ledger_dir` 内(物化视图含查询明文,不许离开明文边界)。
  `_resolve_within_root` 是这条边界唯一实现,`materialize()` 本身也会用它
  校验传入的 `out`(不只是 CLI 层)——即使有人绕过 CLI 直接调用
  `materialize(root, 越界路径)` 也会被拒,这比简报字面"CLI 入口...出強制
  落在 ledger_dir 内"更严格,判断为同一安全意图的合理收紧,已在报告声明。
  **P1(阻断项)**:containment 只挡 `../` 逃逸,不挡"落在 root 内、但撞
  上账本自己文件"这种情形——`out_name="ops.jsonl"` 能通过 containment
  校验,随后 `_write_jsonl_0600` 的 `O_TRUNC` 就会摧毁权威 ops 流。
  `_resolve_within_root` 因此再加一道保留名/保留目录校验(`OutputPathReserved`):
  拒绝与 ops.jsonl/accepted.jsonl/scored.jsonl/aborts.log/meta.json/
  meta.lock/.lock 同名、拒绝匹配 `*.sealed-*.jsonl` 段文件命名模式、
  拒绝落进 `blobs/`/`veccache/` 子目录内部。
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from everos_mcp.ledger import effective_status, iter_rows, read_abort_rids

# ======================================================================
# 常量
# ======================================================================

PIN_KEYS = frozenset({
    "embed_model",
    "rerank_model",
    "model_artifact_fp",
    "tokenizer_artifact_sha",
    "infinity_image_digest",
    "embedding_dim",
    "uv_lock_sha",
    "passage_spec_sha_case",
    "passage_spec_sha_skill",
    "cap",
    "query_budget",
    "scorer_git_sha",
})

_UNHEALTHY_PIN_VALUES = (None, "unknown")

# DoD 门槛(spec §7,推荐默认;放松须经维护者批准,本模块按 spec 冻结值实现)。
_SAMPLE_GATE = 30
_ERROR_RATE_GATE = 0.20
_ONLINE_HEALTH_GATE = 0.90
_FINAL_CLOSURE_GATE = 0.99
_ERROR_STREAK_INCIDENT = 10  # 连续 >= 10 视为事故 -> 门是 streak < 10
_ORPHAN_AGE_SECONDS = 24 * 3600

_FILE_MODE = 0o600
_DIR_MODE = 0o700


class OutputPathEscape(ValueError):
    """物化输出路径解析后落在 `root` 之外——拒绝。物化视图含查询明文,不许
    离开明文边界(与 ledger.py 的账目录权限纪律同一精神)。"""


class OutputPathReserved(ValueError):
    """物化输出路径解析后落在 `root` 内部(未逃逸,containment 校验能通过),
    但撞上了账本自身占用的保留名/保留目录——例如 `out_name="ops.jsonl"`。
    containment 只挡 `../` 逃逸,不挡"落在 root 内但恰好同名"这种情形;
    `_write_jsonl_0600` 用 `O_TRUNC` 写,一旦落到这些名字上就会摧毁账本
    权威源文件(ops/accepted/scored 三条流、aborts.log、checkpoint 的
    meta.json/meta.lock、Ledger 的 `.lock`、段轮转产生的
    `<stream>.sealed-<ts>-<suffix>.jsonl`),或写进 blobstore(`blobs/`)/
    scorer 磁盘向量缓存(`veccache/`)子目录里污染其内容寻址存储。拒绝。"""


# 账本直接落在 root 下的保留文件名(见 ledger.py `Ledger.__init__`/
# `mark_abort`、checkpoint.py `Checkpoint.__init__`)。
_RESERVED_BASENAMES = frozenset({
    "ops.jsonl",
    "accepted.jsonl",
    "scored.jsonl",
    "aborts.log",
    "meta.json",
    "meta.lock",
    ".lock",
})

# 段轮转产生的已封存段文件命名模式(见 ledger.py `LedgerWriter._rotate`附近
# 的 `<name>.sealed-<ts>-<uuid4hex8>.jsonl`)——不锁死 `<name>` 只能是
# ops/accepted/scored,任何 `*.sealed-*.jsonl` 都当保留处理,更保守也更简单。
_SEALED_SEGMENT_PATTERN = re.compile(r"^.+\.sealed-.+\.jsonl$")

# 账本自己使用的子目录(blobstore 内容寻址存储 / scorer 磁盘卡向量缓存)——
# 物化输出不得落进这两个子目录内部的任何位置。
_RESERVED_SUBDIRS = frozenset({"blobs", "veccache"})


# ======================================================================
# per_card 键编码
# ======================================================================

def _card_key(candidate: dict) -> str:
    """`"{card_type}:{card_id}"`——tuple 不能作 JSON key,写死字符串编码。"""
    return f"{candidate.get('card_type')}:{candidate.get('card_id')}"


def _accepted_candidate_keys(accepted_row: dict | None) -> set[str]:
    candidates = (accepted_row or {}).get("candidates") or []
    return {_card_key(c) for c in candidates}


def _collect_numeric_leaves(value) -> list | None:
    """递归收集 `value` 内全部 int/float 叶子节点,返回叶子列表。

    per_card 的分数值形状(Task 7 scorer.py 尚未落地)大概率是
    `{"cos": ..., "ce": ...}` 这种嵌套 dict,而不是裸 float——用递归而不是
    假设扁平结构,对未来实际形状更稳健。

    返回 `None`(不是空列表)表示递归途中遇到了**无法识别的叶子类型**
    (`None`/字符串/其他非数值非容器类型)——这与"容器但一个数值叶子都没有"
    (空 dict/空 list,返回 `[]`)是两种不同的畸形,调用方必须分开处理:
    前者是"分数位置放了杂质",后者是"假装有分数其实是空的",两者都不健康,
    但如果混着判(比如 `None` 也返回 `[]`),空 dict 会跟真的什么都没写的
    值变得无法区分,而这里我们本来就要把两者都判不健康,所以用 `None` 这个
    哨兵只是让语义写清楚,不是为了留一条"None 也算 OK"的后门。
    `bool` 是 `int` 子类但不当分数处理:不算叶子(不进列表),但也不算杂质
    (不触发 `None` 返回)——沿用既有决定,不新增行为。"""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, dict):
        leaves: list = []
        for v in value.values():
            sub = _collect_numeric_leaves(v)
            if sub is None:
                return None
            leaves.extend(sub)
        return leaves
    if isinstance(value, (list, tuple)):
        leaves = []
        for v in value:
            sub = _collect_numeric_leaves(v)
            if sub is None:
                return None
            leaves.extend(sub)
        return leaves
    return None


_REQUIRED_SCORE_KEYS = ("cos", "ce")


def _card_score_valid(value) -> bool:
    """单张卡的 per_card 值是否合法(P1a 收紧,原规则"任意数值叶子存在即可"
    会放行 `{"foo": 1}` 或只有 cos 缺 ce 的畸形行,不是真的"这张卡三信号都
    打完了"):

    - `value` 必须是 dict,且**直接包含 "cos" 和 "ce" 两个键**(两者都要有,
      不接受任意其他键名冒充,也不接受只有其中一个);
    - `cos`/`ce` 各自的值可以是裸数值,也可以是嵌套结构(list/dict)——递归
      展开后必须**至少含一个数值叶子**,且全部数值叶子都 `math.isfinite`。
    """
    if not isinstance(value, dict):
        return False
    if not all(key in value for key in _REQUIRED_SCORE_KEYS):
        return False
    for key in _REQUIRED_SCORE_KEYS:
        leaves = _collect_numeric_leaves(value[key])
        if not leaves:  # None(遇到杂质叶子)或 []([]/{} 递归下来零个数值叶子)
            return False
        if not all(math.isfinite(x) for x in leaves):
            return False
    return True


# ======================================================================
# healthy() —— Task 8 会原样注入 Ledger(scored_validator=healthy)
# ======================================================================

def healthy(scored_row: dict, accepted_row: dict) -> bool:
    """纯函数,零 I/O。签名与 `LedgerWriter` validator 的
    `(row, accepted_row) -> bool` 精确一致,禁止改动签名。"""
    if scored_row.get("status") != "ok":
        return False

    per_card = scored_row.get("per_card")
    if not isinstance(per_card, dict):
        return False
    expected_keys = _accepted_candidate_keys(accepted_row)
    if set(per_card.keys()) != expected_keys:
        return False
    if not all(_card_score_valid(v) for v in per_card.values()):
        return False

    pins = scored_row.get("pins")
    if not isinstance(pins, dict):
        return False
    if not PIN_KEYS.issubset(pins.keys()):
        return False
    for key in PIN_KEYS:
        if pins.get(key) in _UNHEALTHY_PIN_VALUES:
            return False

    return True


# ======================================================================
# fold()
# ======================================================================

def fold(scored_rows: list[dict], accepted_row: dict) -> dict | None:
    """健康行中 attempt_no 最大;无则最新 permanent_failure;再无则最新
    retryable_error;三档都没有 -> None。"""
    healthy_rows = [r for r in scored_rows if healthy(r, accepted_row)]
    if healthy_rows:
        return max(healthy_rows, key=lambda r: r.get("attempt_no", -1))

    permanent = [r for r in scored_rows if r.get("status") == "permanent_failure"]
    if permanent:
        return max(permanent, key=lambda r: r.get("attempt_no", -1))

    retryable = [r for r in scored_rows if r.get("status") == "retryable_error"]
    if retryable:
        return max(retryable, key=lambda r: r.get("attempt_no", -1))

    return None


# ======================================================================
# score_eligible()
# ======================================================================

# ======================================================================
# 一查询一行的标定输入字段(P2/R4 阻断项 #2)——materialize() 输出行必须是
# "一查询一行的标定输入",不能只有 status/health/fold 元数据。
# ======================================================================

# 从 accepted 行原样搬进物化行的字段——刻意不含 everos_rid(任务简报未列入
# 这份清单,判别联合语义决定了它是否存在,但物化行不主动暴露)。判别联合
# 语义原样保留:某字段在该 stage 的 accepted 行里本就缺席(如 contract_reject
# 没有 candidates/search_ms),物化行也缺席,不用 None 占位假装"有这个字段"
# ——与 `ledger.accepted_row` 的判别联合纪律(禁止伪值糊账)一致。
_ACCEPTED_CALIBRATION_FIELDS = (
    "query", "q_len", "error_code", "search_ms", "config_fp",
    "candidates", "returned_ids",
)


def _enrich_from_accepted(row_out: dict, accepted_row: dict | None) -> None:
    """把 accepted 行里的标定字段原样搬进物化行。`accepted_row` 为 None
    (accepted 落账彻底失败,只有 ops started+terminal)时无字段可搬,物化行
    只保留 rid/traffic_class/effective_status 这些基础信封字段。"""
    if accepted_row is None:
        return
    for key in _ACCEPTED_CALIBRATION_FIELDS:
        if key in accepted_row:
            row_out[key] = accepted_row[key]


def _enrich_from_folded_scored(row_out: dict, folded: dict | None) -> None:
    """把折叠选中的 scored 行(健康优先,退化到 permanent_failure/
    retryable_error)的标定字段原样搬进物化行——`per_card`/`pins`/`producer`/
    `attempt_no`/`score_error_code`。`folded is None`(从未打过分,如新鲜孤儿)
    时整组字段缺席,不糊空字典/占位值。"""
    if folded is None:
        return
    row_out["per_card"] = folded.get("per_card")
    row_out["pins"] = folded.get("pins")
    row_out["producer"] = folded.get("producer")
    row_out["attempt_no"] = folded.get("attempt_no")
    if folded.get("score_error_code") is not None:
        row_out["score_error_code"] = folded["score_error_code"]


def score_eligible(effective: str, accepted_row: dict | None) -> bool:
    """`effective=="hit"` 且候选非空。`accepted_row` 缺失(数据损坏/未落账)
    视为不可打分,不 raise——物化是离线只读分析层,不应该因为一行数据形状
    异常就整体崩溃。"""
    if accepted_row is None:
        return False
    return effective == "hit" and bool(accepted_row.get("candidates"))


# ======================================================================
# 输出路径边界
# ======================================================================

def _resolve_within_root(root: Path, out) -> Path:
    """把 `out`(可以是相对名或 Path)解析为绝对路径,并校验它落在
    `root` 内部,否则 raise `OutputPathEscape`。相对路径按"拼在 root 下"
    处理;绝对路径也照样校验包含关系,不因为调用方传了绝对路径就放行。

    containment 通过之后还有第二道校验:相对路径的**每一段** component
    (不只是最终 basename)都不得撞上账本自身的保留名(`_RESERVED_BASENAMES`)、
    已封存段文件命名模式(`_SEALED_SEGMENT_PATTERN`)或保留子目录
    (`_RESERVED_SUBDIRS`),否则 raise `OutputPathReserved`。这两道校验语义
    不同:containment 挡"逃出 root"，保留名/目录校验挡"落在 root 内、但会
    覆盖/污染账本自己的文件"(如 `out_name="ops.jsonl"` 这种 containment
    会放行、但 `O_TRUNC` 会摧毁权威 ops 流的情形)。

    **P1(阻断项,第二轮外部审查)**:只查 basename(最终段)会被
    `out_name="aborts.log/view.jsonl"` 绕过——basename 是 `view.jsonl`
    (无害),但父目录段 `aborts.log` 不存在时 `_write_jsonl_0600` 的
    `path.parent.mkdir(parents=True, ...)` 会把它**创建成一个目录**,
    永久摧毁 `ledger.py` `mark_abort()` 期望在那里的普通文件(`O_APPEND`
    写从此永远失败)。因此必须遍历 `relative.parts` 的**每一段**,而不是
    只看最后一段。"""
    root_resolved = Path(root).resolve()
    out_path = Path(out)
    candidate = out_path if out_path.is_absolute() else root_resolved / out_path
    candidate_resolved = candidate.resolve()
    try:
        relative = candidate_resolved.relative_to(root_resolved)
    except ValueError:
        raise OutputPathEscape(
            f"materialize 输出 {out!r} 解析后为 {candidate_resolved},"
            f"落在 root {root_resolved} 之外——拒绝(物化视图含查询明文,"
            "不许离开明文边界)"
        ) from None

    for part in relative.parts:
        if part in _RESERVED_BASENAMES or _SEALED_SEGMENT_PATTERN.match(part):
            raise OutputPathReserved(
                f"materialize 输出 {out!r} 解析后为 {candidate_resolved},"
                f"路径段 {part!r} 与账本保留文件同名——拒绝(不管它出现在"
                "basename 还是中间目录段,都可能覆盖账本源文件,或者——"
                "如该保留名本该是文件却被当成目录段创建——摧毁账本对它的"
                "文件写入路径)"
            )
        if part in _RESERVED_SUBDIRS:
            raise OutputPathReserved(
                f"materialize 输出 {out!r} 解析后为 {candidate_resolved},"
                f"落在保留子目录 {part!r} 内——拒绝"
                "(该目录属于账本自身的 blobstore/向量缓存)"
            )
    return candidate_resolved


def _write_jsonl_0600(path: Path, rows: list[dict]) -> None:
    """0600 创建物化输出文件(与 ledger.py 账文件同权限纪律)。"""
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=_DIR_MODE)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, _FILE_MODE)
    try:
        os.chmod(path, _FILE_MODE)  # umask 可能已冲掉 os.open 的 mode
        for row in rows:
            line = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            os.write(fd, line)
    finally:
        os.close(fd)


# ======================================================================
# Stats
# ======================================================================

@dataclass
class Stats:
    total_real: int
    non_error_real: int  # real 且 effective∈{hit,abstain_empty};≥30 是独立样本门
    error_count: int
    error_rate: float  # error_count / total_real(按 ops effective_status)
    H: int  # effective_status=="hit" 的 real 查询数(score_eligible 全集)
    online_health_count: int  # producer=realtime 且 healthy 的查询数
    online_health_rate: float  # online_health_count / H(H==0 时记 0.0)
    final_closure_count: int  # 任意 producer healthy 的查询数
    final_closure_rate: float  # final_closure_count / H(H==0 时记 0.0)
    permanent_failure_count: int
    orphan_count: int  # >24h 无终态的 score_eligible 查询数
    max_consecutive_score_error_streak: int
    dod_pass: bool
    parse_warnings: int = 0


# ======================================================================
# materialize()
# ======================================================================

def materialize(root: Path, out) -> Stats:
    root = Path(root)
    out_resolved = _resolve_within_root(root, out)

    ops_rows, w1 = iter_rows(root, "ops")
    accepted_rows, w2 = iter_rows(root, "accepted")
    scored_rows, w3 = iter_rows(root, "scored")
    abort_rids = read_abort_rids(root)
    parse_warnings = w1 + w2 + w3

    now = time.time()

    # 每个 rid 唯一的 ops started 行是查询存在性与 traffic_class 的单一真源
    # ——即使主账/scored 全部写失败,started 行也一定在(started 写不进是
    # unit fail-stop,不存在"有流量却没有这行"的状态)。
    started_by_rid: dict[str, dict] = {}
    for row in ops_rows:
        if row.get("kind") == "started":
            started_by_rid[row.get("rid")] = row

    accepted_by_rid: dict[str, dict] = {}
    for row in accepted_rows:
        if row.get("kind") == "accepted":
            accepted_by_rid[row.get("rid")] = row

    real_rids = [rid for rid, row in started_by_rid.items() if row.get("traffic_class") == "real"]
    # 确定性输出顺序:按 started ts 排序。
    real_rids.sort(key=lambda rid: (started_by_rid[rid].get("ts", 0), rid))

    total_real = len(real_rids)
    error_count = 0
    h_rids: list[str] = []
    online_health_count = 0
    final_closure_count = 0
    permanent_failure_count = 0
    orphan_count = 0

    output_rows: list[dict] = []

    for rid in real_rids:
        effective = effective_status(ops_rows, accepted_rows, abort_rids, rid)
        accepted = accepted_by_rid.get(rid)

        row_out = {
            "rid": rid,
            "traffic_class": "real",
            "ts": started_by_rid[rid].get("ts"),
            "effective_status": effective,
        }
        # P2(R4 #2):物化视图必须是"一查询一行的标定输入",不能只有
        # status/health/fold 元数据——把 accepted 行原样携带的标定字段搬进来。
        # 判别联合语义原样保留:accepted 为 None,或某字段在该 stage 本就
        # 缺席时,物化行也缺席对应键(见 `_enrich_from_accepted`)。
        _enrich_from_accepted(row_out, accepted)

        if effective == "error":
            error_count += 1
            row_out.update({
                "score_eligible": False,
                "healthy_final": False,
                "permanent_failure": False,
                "orphan": False,
            })
            output_rows.append(row_out)
            continue

        if effective == "hit":
            h_rids.append(rid)
            eligible = score_eligible(effective, accepted)
            row_out["score_eligible"] = eligible

            if eligible:
                scored_for_rid = [r for r in scored_rows if r.get("rid") == rid]
                final_healthy = any(healthy(r, accepted) for r in scored_for_rid)
                online_healthy = any(
                    r.get("producer") == "realtime" and healthy(r, accepted)
                    for r in scored_for_rid
                )
                folded = fold(scored_for_rid, accepted)

                if online_healthy:
                    online_health_count += 1
                if final_healthy:
                    final_closure_count += 1
                    is_permanent_failure = False
                    is_orphan = False
                elif folded is not None and folded.get("status") == "permanent_failure":
                    permanent_failure_count += 1
                    is_permanent_failure = True
                    is_orphan = False
                else:
                    is_permanent_failure = False
                    age = now - accepted.get("ts", now)
                    is_orphan = age > _ORPHAN_AGE_SECONDS
                    if is_orphan:
                        orphan_count += 1

                row_out.update({
                    "healthy_final": final_healthy,
                    "online_healthy": online_healthy,
                    "permanent_failure": is_permanent_failure,
                    "orphan": is_orphan,
                    "folded_status": folded.get("status") if folded else None,
                    "folded_attempt_no": folded.get("attempt_no") if folded else None,
                    "folded_score_error_code": folded.get("score_error_code") if folded else None,
                })
                # 折叠选中的 scored attempt(健康优先,退化到 permanent_failure/
                # retryable_error)的完整标定字段——per_card/pins/producer/
                # attempt_no/score_error_code,folded is None(从未打过分)时
                # 整组缺席。
                _enrich_from_folded_scored(row_out, folded)
            else:
                row_out.update({
                    "healthy_final": False,
                    "permanent_failure": False,
                    "orphan": False,
                })
        else:
            # abstain_empty:无候选可打分,不进 H,也不算 orphan/pf。
            row_out.update({
                "score_eligible": False,
                "healthy_final": False,
                "permanent_failure": False,
                "orphan": False,
            })

        output_rows.append(row_out)

    H = len(h_rids)
    non_error_real = total_real - error_count
    error_rate = (error_count / total_real) if total_real else 0.0
    online_health_rate = (online_health_count / H) if H else 0.0
    final_closure_rate = (final_closure_count / H) if H else 0.0

    max_streak = _max_consecutive_score_error_streak(
        scored_rows, accepted_by_rid, set(real_rids)
    )

    dod_pass = (
        H > 0
        and non_error_real >= _SAMPLE_GATE
        and error_rate <= _ERROR_RATE_GATE
        and online_health_count >= math.ceil(_ONLINE_HEALTH_GATE * H)
        and final_closure_count >= math.ceil(_FINAL_CLOSURE_GATE * H)
        and max_streak < _ERROR_STREAK_INCIDENT
        and orphan_count == 0  # P1b:DoD 文本"无 age>24h 非终态 score_eligible accepted 行"
    )

    stats = Stats(
        total_real=total_real,
        non_error_real=non_error_real,
        error_count=error_count,
        error_rate=error_rate,
        H=H,
        online_health_count=online_health_count,
        online_health_rate=online_health_rate,
        final_closure_count=final_closure_count,
        final_closure_rate=final_closure_rate,
        permanent_failure_count=permanent_failure_count,
        orphan_count=orphan_count,
        max_consecutive_score_error_streak=max_streak,
        dod_pass=dod_pass,
        parse_warnings=parse_warnings,
    )

    _write_jsonl_0600(out_resolved, output_rows)
    return stats


def _max_consecutive_score_error_streak(
    scored_rows: list[dict], accepted_by_rid: dict[str, dict], real_rids: set[str]
) -> int:
    """同 score_error_code 连续最大值:成功(健康 ok)重置**全部**计数器;
    失败按各自 score_error_code 独立计数,不同因的失败穿插进来不重置其他
    码的计数(每个 code 一个独立的连续计数器,只在遇到成功时全部清零)。

    时间线只取 traffic_class=="real" 的 rid,按 scored 行的 written_ts 排序
    ——这是打分链路本身的健康信号(是否持续同因失败),不区分查询边界。"""
    relevant = [r for r in scored_rows if r.get("rid") in real_rids]
    relevant.sort(key=lambda r: (r.get("written_ts", 0), r.get("attempt_no", 0)))

    streaks: dict[str, int] = {}
    max_streak = 0
    for row in relevant:
        accepted = accepted_by_rid.get(row.get("rid"), {})
        if healthy(row, accepted):
            streaks.clear()
            continue
        code = row.get("score_error_code")
        if code is None:
            continue
        streaks[code] = streaks.get(code, 0) + 1
        max_streak = max(max_streak, streaks[code])

    return max_streak


# ======================================================================
# CLI: python -m everos_mcp.materialize <ledger_dir> <out_name>
# ======================================================================

def _main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: python -m everos_mcp.materialize <ledger_dir> <out_name>",
            file=sys.stderr,
        )
        return 2
    root = Path(argv[1])
    out_name = argv[2]
    try:
        stats = materialize(root, out_name)
    except (OutputPathEscape, OutputPathReserved) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(dataclasses.asdict(stats), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
