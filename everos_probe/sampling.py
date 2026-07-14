"""§4 分层抽样：source × tool-round-bucket(<3/3-5/6+)，per-stratum floor=5，
层内 hash(external_id) 无偏随机（非 rowid），选中行冻结落盘供 Phase B 只读喂料。

真实库分层占比 wᵢ 与候选池在同一次全量扫描（scan_target_conversations）中产出——
两者共享同一份"逐会话读消息、算 round bucket"的工作，避免扫两遍。

⚠️ spec §8 R1 与 §5 存在一处未同步措辞（详见 plan 头部说明）：本模块按 §5 的最终定义
实现——tool 行 extra 全空的坏会话在这里（抽样阶段）就被剔除出候选池，因此天然不会进入
后续的喂料/统计管线，不入分母不入分子。
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from cass_corpus.reader import coerce_tool_call_id, extra_dict

TOOL_ROUND_BUCKETS = ("<3", "3-5", "6+")
_EXTRA_COLS = ["extra_bin", "extra_json"]


def tool_round_bucket(n_rounds: int) -> str:
    if n_rounds < 3:
        return "<3"
    if n_rounds <= 5:
        return "3-5"
    return "6+"


def count_tool_rounds(rows: list[dict]) -> int:
    """本地对齐 everalgo._count_tool_call_rounds 的代理指标——数「会被 role_map 映射成
    ToolCallRequest」的 CASS tool_call 行,即 role=="tool_call" 且能从 extra 解出非空
    tool_call_id 的行(无 id 的降级为 synthetic assistant 文本,不是 ToolCallRequest,
    everalgo 不会数它)。这是 probe_calibrate 前的初始代理口径,真样本前须用 3 个已知
    结局合成会话对齐 EverOS 日志 `only N rounds` 的真实计数(spec §3/R5),不一致则改口径
    (见 Phase B Task 6 `scripts/probe_calibrate_m1b.py`)。"""
    count = 0
    for r in rows:
        if r.get("role") != "tool_call":
            continue
        ex = extra_dict(r, _EXTRA_COLS) or {}
        if coerce_tool_call_id(ex.get("tool_call_id")) is not None:
            count += 1
    return count


def normalize_source(agent_slug: "str | None") -> "str | None":
    """3 目标来源(spec §2/§4，M1a"目标三家")：claude_code / codex / openclaw
    (含 openclaw/* 子 agent 归一为 openclaw)。非目标来源(gemini/pi_agent 等)返回 None，
    抽样阶段排除。"""
    if not agent_slug:
        return None
    if agent_slug in ("claude_code", "codex"):
        return agent_slug
    if agent_slug.split("/")[0] == "openclaw":
        return "openclaw"
    return None


def stratum_key(source: str, bucket: str) -> str:
    return f"{source}|{bucket}"


def stable_hash(external_id: str) -> int:
    """跨进程/跨运行确定性哈希(spec §4 R0-I2「层内 hash(external_id) 无偏抽,非 rowid」)。
    Python 内置 hash() 对 str 受 PYTHONHASHSEED 随机化影响,同一 external_id 在不同进程/
    不同次运行会得到不同值——用它会让抽样结果不可复现(违反 §11「external_id + 层 + 快照
    路径可复现」)。改用 sha256 定长摘要转 int,与 PYTHONHASHSEED 无关。"""
    return int(hashlib.sha256(external_id.encode("utf-8")).hexdigest(), 16)


def has_pairable_extra(rows: list[dict]) -> bool:
    """spec §8 R1「抽样命中 extra_bin 全空坏会话 -> adapter 拿不到配对 id」的判据：
    整会话所有 tool_call/tool_result 行是否至少有一条能解出有效 tool_call_id。
    全空 -> 坏会话,抽样阶段剔除(不进候选池)。无 tool_call/tool_result 行本身不算
    "全空坏会话"(真会话可能就是无工具调用,会在 EverOS 结构门被正常拒,不是本函数要
    剔除的"数据损坏")；只有"有 tool 行但全无可解 id"才是数据损坏信号。"""
    tool_rows = [r for r in rows if r.get("role") in ("tool_call", "tool_result")]
    if not tool_rows:
        return True
    for r in tool_rows:
        ex = extra_dict(r, _EXTRA_COLS) or {}
        if coerce_tool_call_id(ex.get("tool_call_id")) is not None:
            return True
    return False


@dataclass(frozen=True)
class ConvMeta:
    conversation_id: int
    external_id: str
    source: str
    n_rounds: int
    bucket: str
    stratum: str


@dataclass
class LibraryScan:
    strata: dict
    excluded_empty_extra: int
    skipped_no_external_id: int
    total_target_source_conversations: int


_ROW_SQL = (
    "SELECT idx, role, content, created_at, extra_bin, extra_json "
    "FROM messages WHERE conversation_id = ? ORDER BY idx ASC"
)

_CONV_SQL = """
SELECT c.id AS id, a.slug AS agent, c.external_id AS external_id
FROM conversations c
JOIN agents a ON a.id = c.agent_id
JOIN messages m ON m.conversation_id = c.id
GROUP BY c.id
HAVING COUNT(m.id) > 0
"""


def fetch_rows(con: sqlite3.Connection, conversation_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(_ROW_SQL, (conversation_id,)).fetchall()]


def scan_target_conversations(db_path: str) -> LibraryScan:
    """mode=ro 全量扫描目标三源会话,逐会话读消息算 tool round bucket。一次扫描同时
    供「真实分层占比 wᵢ」(spec §5/§6)与「候选池」两用。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        have = {r["name"] for r in con.execute("PRAGMA table_info(conversations)")}
        if "external_id" not in have:
            raise RuntimeError(
                "scan_target_conversations: schema has no external_id column "
                "(legacy schema unsupported — sampling needs a stable session key)"
            )
        strata: dict = collections.defaultdict(list)
        excluded_empty_extra = 0
        skipped_no_eid = 0
        total = 0
        for c in con.execute(_CONV_SQL).fetchall():
            source = normalize_source(c["agent"])
            if source is None:
                continue
            total += 1
            eid = c["external_id"]
            if not eid:
                skipped_no_eid += 1
                continue
            rows = fetch_rows(con, c["id"])
            if not has_pairable_extra(rows):
                excluded_empty_extra += 1
                continue
            n_rounds = count_tool_rounds(rows)
            bucket = tool_round_bucket(n_rounds)
            stratum = stratum_key(source, bucket)
            strata[stratum].append(ConvMeta(c["id"], eid, source, n_rounds, bucket, stratum))
        return LibraryScan(dict(strata), excluded_empty_extra, skipped_no_eid, total)
    finally:
        con.close()


def stratum_shares(scan: LibraryScan) -> dict:
    """真实库分层占比 wᵢ(spec §4/§5/§6)。

    Controller 裁决②(2026-07-13 拍板)：分母 = 已通过 has_pairable_extra 且有
    external_id 的候选池,不含 excluded_empty_extra(extra 全空坏样本)与
    skipped_no_external_id(无 external_id)。此处直接对 scan.strata 求占比即天然满足
    该口径——被排除/跳过的会话根本没有 ConvMeta 进 scan.strata,不会被计入 total。
    拍板理由:坏样本 extra 全空 -> 无配对 id -> count_tool_rounds 全算 0 轮 -> 会全部
    落入 `<3` 桶,污染分层占比;把它们排除在候选池分母之外更 sound,且与 spec §5
    「剔除=未观测」同向(不是"观测到 0 轮",而是"根本没观测到")。"""
    sizes = {k: len(v) for k, v in scan.strata.items()}
    total = sum(sizes.values())
    if total == 0:
        raise ValueError("empty library scan: no eligible target-source conversations")
    return {k: v / total for k, v in sizes.items()}


def compute_quotas(strata_sizes: dict, target_n: int, floor: int = 5) -> dict:
    """spec §4：真实占比比例分配 + 每个存在的格 floor=5(population < floor 时封顶到
    population)。剩余名额用逐名额最大余数法分配——每轮把 1 个名额发给"当前离其比例
    理想值最远且未封顶"的格,直到打平 target_n 或所有格全部封顶为止。

    2026-07-13 controller 裁决重写:原「整数截断 raw[k] + stall_guard 补漏」两段式实现
    有真实取整损失 bug——某格比例份额的截断值超过它实际能吃下的容量(population)时,
    超出部分被 `quota[k]=min(existing[k],quota[k]+add[k])` 悄悄吃掉,而这些"浪费"的名额
    因已计入 `leftover=remaining-sum(add)` 而永不重分配给其他还有余量的格,导致总配额
    悄悄小于 min(total_pop,target_n)(9 格 fuzz 实测 43% 场景会少发,最坏缺 4)。改成本
    函数的单发-循环实现从根上避免这个损失:候选池每轮都用「当前是否仍未封顶」实时
    过滤,一个名额发完才决定下一个发给谁,不需要"整数截断+补漏"两段式,也不需要原来的
    stall_guard 启发式。"""
    existing = {k: v for k, v in strata_sizes.items() if v > 0}
    if not existing:
        raise ValueError("no non-empty strata to sample from")

    quota = {k: min(floor, v) for k, v in existing.items()}
    remaining = target_n - sum(quota.values())
    if remaining <= 0:
        return quota   # floor 优先，硬性下限，不因 target_n 更小而降

    total_pop = sum(existing.values())
    shares = {k: v / total_pop for k, v in existing.items()}
    ideal = {k: shares[k] * remaining for k in existing}   # 该格在 remaining 池里的理想份额
    extra = {k: 0 for k in existing}

    for _ in range(remaining):
        candidates = [k for k in existing if quota[k] + extra[k] < existing[k]]
        if not candidates:
            break   # 全部封顶,population 不够吃满 target_n(§4：population 封顶允许总量 < target_n)
        best = max(candidates, key=lambda k: ideal[k] - extra[k])
        extra[best] += 1

    for k in existing:
        quota[k] += extra[k]
    return quota


def select_sample(scan: LibraryScan, quotas: dict) -> dict:
    """每层按 stable_hash(external_id) 升序排序取前 quota 个(§4：层内 hash 无偏随机,
    非 rowid；sha256 输出均匀分布,与 external_id 的字典序/时间序无关)。"""
    selected = {}
    for stratum, quota in quotas.items():
        members = sorted(scan.strata.get(stratum, []), key=lambda m: stable_hash(m.external_id))
        selected[stratum] = members[:quota]
    return selected


def _row_to_json(row: dict) -> dict:
    out = dict(row)
    eb = out.get("extra_bin")
    if isinstance(eb, (bytes, bytearray)):
        out["extra_bin"] = {"__b64__": base64.b64encode(bytes(eb)).decode("ascii")}
    return out


def _row_from_json(row: dict) -> dict:
    out = dict(row)
    eb = out.get("extra_bin")
    if isinstance(eb, dict) and "__b64__" in eb:
        out["extra_bin"] = base64.b64decode(eb["__b64__"])
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def freeze_snapshot(out_path: str, shares: dict, selected: dict, rows_by_conv: dict) -> None:
    """落盘快照(§3/§4/§11)：真实库分层占比 wᵢ + 每层选中 external_id 清单 + 每选中会话
    的完整消息行(供后续 feed 只读快照,不再碰 CASS 活库,硬约束)。原子写入(tmp+replace)。

    Controller 裁决③(2026-07-13 拍板)：`rows_by_conv` 用 external_id 作字典键,隐含
    假设选中会话的 external_id 互不相同。若两个不同 ConvMeta(哪怕分属不同层)撞了同一个
    external_id,静默覆盖会丢一条快照数据且事后无从察觉——fail-loud,报出撞的
    external_id。"""
    seen_eids: dict = {}
    for stratum, members in selected.items():
        for m in members:
            if m.external_id in seen_eids:
                raise ValueError(
                    f"freeze_snapshot: duplicate external_id {m.external_id!r} in selected "
                    f"(strata {seen_eids[m.external_id]!r} and {stratum!r}) — "
                    "rows_by_conv is keyed by external_id and would silently lose one snapshot row"
                )
            seen_eids[m.external_id] = stratum

    manifest = {
        "sampled_at": _now_iso(),
        "library_stratum_shares": shares,
        "strata": {
            stratum: [
                {
                    "external_id": m.external_id,
                    "source": m.source,
                    "bucket": m.bucket,
                    "n_rounds": m.n_rounds,
                    "conversation_id": m.conversation_id,
                    "rows": [_row_to_json(r) for r in rows_by_conv[m.external_id]],
                }
                for m in members
            ]
            for stratum, members in selected.items()
        },
    }
    tmp = f"{out_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)


def load_snapshot(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    for members in manifest["strata"].values():
        for m in members:
            m["rows"] = [_row_from_json(r) for r in m["rows"]]
    return manifest


def run_sampling(db_path: str, out_path: str, target_n: int = 90, floor: int = 5) -> dict:
    """Phase B Task 7 的唯一入口：scan -> shares -> quotas -> select -> 取选中会话完整行
    -> 落快照。全程 mode=ro,不修改 CASS。"""
    scan = scan_target_conversations(db_path)
    shares = stratum_shares(scan)
    sizes = {k: len(v) for k, v in scan.strata.items()}
    quotas = compute_quotas(sizes, target_n, floor)
    selected = select_sample(scan, quotas)

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows_by_conv = {}
        for members in selected.values():
            for m in members:
                rows_by_conv[m.external_id] = fetch_rows(con, m.conversation_id)
    finally:
        con.close()

    freeze_snapshot(out_path, shares, selected, rows_by_conv)
    return {
        "out_path": out_path,
        "shares": shares,
        "quotas": quotas,
        "selected_counts": {k: len(v) for k, v in selected.items()},
        "excluded_empty_extra": scan.excluded_empty_extra,
        "skipped_no_external_id": scan.skipped_no_external_id,
        "total_target_source_conversations": scan.total_target_source_conversations,
    }
