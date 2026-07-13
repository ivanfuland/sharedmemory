"""把适配器的各段真正串起来。

codex R0 指出：`absorb_orphans` / `split_feedable` / `ensure_unique_timestamps`
若只被定义和单测，而不进 feed 管线，就是死代码 ——
测试全绿却什么都没做。本模块是它们唯一的编排入口。

⚠️ 不含结构门预筛（原 Task 3，已于 2026-07-13 删除——EverOS 自带该门，
镜像必漂移，详见文末「执行修订」）。本模块只做 absorb/split/ts 的确定性格式整理，
是否喂给 everalgo 由 EverOS 自己的结构门 + 语义门判定。

顺序不可调换：
    read -> map -> absorb_orphans -> split_feedable
         -> ensure_unique_timestamps -> feed
- absorb 必须在 split 之前：先合并相邻孤儿再判定「哪些留在尾部」。
- ensure_unique_timestamps 只作用于 split_feedable 判出的可喂部分，held 的尾部孤儿
  不参与时间戳整理与后续 feed。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from everos_adapter.batching import ensure_unique_timestamps
from everos_adapter.cap import Clamper, make_clamper
from everos_adapter.cass_reader import read_conversation
from everos_adapter.feed import feed_session
from everos_adapter.role_map import map_to_add_messages
from everos_adapter.text_strategy import absorb_orphans, split_feedable

# read_conversation 的 extra_cols：extra_bin 优先、extra_json 兜底（cass_corpus.reader
# ._extra_dict 的既定约定，见 test_cass_reader.py / test_role_map.py）。真实配对数据
# 绝大多数在 extra_bin 顶层，但只取 extra_bin 会让 extra_dict 在 extra_bin IS None 时
# 短路、永不读 row["extra_json"] —— 任何 extra_bin 为空而 extra_json 有数据的行会被
# 静默丢弃（无报错无日志）。生产读取范围不能为了迁就手写 fixture 而收窄；调用方
# （含本模块测试）的行 dict 需同时带 extra_bin/extra_json 两个 key。
_EXTRA_COLS = ["extra_bin", "extra_json"]


@dataclass
class PreparedSession:
    session_id: str
    feedable: list[dict] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)
    skip_reason: str = ""

    @property
    def should_feed(self) -> bool:
        return not self.skip_reason and bool(self.feedable)


def prepare_session(
    session_id: str,
    rows: list[dict],
    agent_id: str,
    user_sender: str,
    clamper: Clamper | None = None,
    tool_result_cap: int = 1500,
) -> PreparedSession:
    """纯函数：不发任何 HTTP（但需要 clamper —— 被吸收的 orphan 必须在 append 前压好，
    codex R1 P1#1）。不含结构门预筛：是否喂给 everalgo 由 EverOS 自己判定，本函数只做
    absorb/split/ts 的格式整理。`ensure_unique_timestamps` 永不抛出（skew-quarantine
    机制已删除，2026-07-13 项目决策：不丢数据），故 `should_feed` 为 False 的唯一情形是
    `feedable` 为空（如整会话被 absorb/hold 后无内容）。"""
    clamper = clamper or make_clamper()
    mapped = map_to_add_messages(read_conversation(rows, _EXTRA_COLS), agent_id, user_sender)
    absorbed = absorb_orphans(mapped, clamp_fn=lambda c: clamper.clamp(c, tool_result_cap))
    feedable, held = split_feedable(absorbed)
    feedable = ensure_unique_timestamps(feedable)

    return PreparedSession(session_id, feedable, held, "")


def run_session(
    base_url: str,
    session_id: str,
    rows: list[dict],
    agent_id: str,
    user_sender: str,
    clamper: Clamper | None = None,
    tool_result_cap: int = 1500,
) -> dict:
    clamper = clamper or make_clamper()
    ps = prepare_session(
        session_id, rows, agent_id, user_sender,
        clamper=clamper, tool_result_cap=tool_result_cap,
    )
    if not ps.should_feed:
        # 无可喂消息：一个 HTTP 都不发。结构门 / 语义门交给 EverOS 自己判定。
        return {"skipped": True, "reason": ps.skip_reason or "no feedable messages",
                "held": len(ps.held), "result": None}

    result = feed_session(base_url, session_id, ps.feedable,
                          clamper=clamper, tool_result_cap=tool_result_cap)
    return {"skipped": False, "reason": "", "held": len(ps.held), "result": result}
