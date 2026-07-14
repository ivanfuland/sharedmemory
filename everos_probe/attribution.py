"""解析 EverOS 服务端日志 + 绑卡(scan_terminal)，把每个已喂会话分类为
pass / structural_reject / semantic_reject / other（spec §5「未过门拆 结构门拒 /
语义门拒 / 其他（解析 EverOS 日志）」）。

日志正则原样摘自 EverOS 真实产出（拿来主义：不重写、不"改进"上游的判据文本，
逐字节对齐真实产出。全部经真跑 `configure_logging` + 实际 logger 调用核对，含
ConsoleRenderer 的 ANSI 着色不影响子串匹配）：

来自 everalgo.agent_memory.case（vendored dependency，stdlib logging，%s 格式串）：
  结构门 Step 1  logger.info("no items on memcell (n_items=0), skipping")
  结构门 Step 3  logger.info("skipping memcell (n_items=%d): %s", n_items, reason)
  结构门 Step 3b logger.info("skipping memcell — only %d tool-call rounds < min %d", rounds, min_)
  语义门 Step 7  logger.info("filtered out by LLM: %s", reason)

来自 everos.memory.strategies.extract_agent_case（src/everos，structlog event）：
  结构门         logger.warning("agent_case_skipped_no_assistant", memcell_id=..., session_id=...)
    —— 会话 memcell 里没有任何 assistant 发言人，agent_case 抽取结构性跳过（真跑确认
       是 other 的主要来源）。

来自 everalgo.agent_memory.case（同上 stdlib logger，_compress_experience 第 626/629 行）：
  语义门         logger.info("LLM returned empty 'task_intent', skipping")
  语义门         logger.warning("LLM returned empty 'approach', skipping")
    —— LLM 抽取阶段判定没有有效任务意图/方法而跳过，语义上是"LLM 判定不值得" ->
       归 semantic_reject。
"""
from __future__ import annotations

import re

from everos_adapter.scan_terminal import session_case_entry_ids

_STRUCTURAL_PATTERNS = (
    re.compile(r"no items on memcell \(n_items=0\), skipping"),
    re.compile(r"skipping memcell \(n_items=\d+\): .*"),
    re.compile(r"skipping memcell — only \d+ tool-call rounds < min \d+"),
    re.compile(r"agent_case_skipped_no_assistant"),
)
_SEMANTIC_PATTERNS = (
    re.compile(r"filtered out by LLM: ?(?P<reason>.*)"),
    re.compile(r"LLM returned empty '(?:task_intent|approach)', skipping"),
)
# ⚠ 以上正则是对 EverOS 真实日志文本的转录（含 U+2014 em dash），不是从源码里机械
# 提取的。实现时已重新对照当时的源码逐字核对一遍——结构门 4 条对 `everalgo/agent_memory
# /case.py` 第 106/116/124/604 行 + `everos/memory/strategies/extract_agent_case.py`
# 第 62 行(event 名),语义门 2 条另对 `case.py` 第 604/626/629 行(含 `_compress_experience`
# 内的 task_intent/approach 空值分支)——上游措辞可能已漂移，需在下次真跑前再核一遍。
# 这不是唯一防线：`probe_calibrate`（§3 已知结局合成会话）是运行时的 backstop，正则
# 测漏/测错会在那一步被三选一的错误分类当场抓到，而不是喂到真样本才发现。

PASS = "passed"
STRUCTURAL_REJECT = "structural_reject"
SEMANTIC_REJECT = "semantic_reject"
OTHER = "other"


def classify_log_window(log_text: str) -> str:
    """只判该窗口文本里出现的门拒信号，不看卡产出(那是 scan_terminal 的职责，由
    classify_session 合并)。无信号 -> "" 空串（供 classify_session 落 OTHER）。

    结构优先于语义：先扫全部结构门正则，命中即返回 STRUCTURAL_REJECT，不再看语义门。
    理论上一个日志窗口不该同时出现两种信号，但仲裁顺序仍需明确（见 test_attribution.py
    的 test_classify_log_window_structural_wins_when_both_signals_present）。"""
    for pat in _STRUCTURAL_PATTERNS:
        if pat.search(log_text):
            return STRUCTURAL_REJECT
    for pat in _SEMANTIC_PATTERNS:
        if pat.search(log_text):
            return SEMANTIC_REJECT
    return ""


def classify_session(log_window_text: str, case_entry_ids: list) -> str:
    """spec §5：过门 = 至少产出 1 张绑本 session_id 的卡，优先于日志信号（即便日志同时
    出现拒绝噪声——理论上不该同时发生，但产出卡是更强的终态判据，优先采信）。未过门时
    按日志窗口拆结构门拒/语义门拒；两者都没有 -> other（含 Step 8 compress 失败、LLM
    报错等未覆盖分支）。"""
    if case_entry_ids:
        return PASS
    reason = classify_log_window(log_window_text)
    return reason or OTHER


def read_log_window(log_path: str, start_offset: int, end_offset: int) -> str:
    """读日志文件 [start_offset, end_offset) 字节窗口（Phase B 串行喂料时，每个会话
    feed 前记 start_offset=文件当前大小，flush+等待终态后记 end_offset，供本会话专属
    日志切片——串行保证窗口不与其他会话交叉）。

    **必须二进制读**：EverOS 日志的 reason/摘要文本可能含中文，UTF-8 是变长编码。文本
    模式 open 后 `seek(byte_offset)` 可以按字节定位，但随后的 `read(n)` 按**字符数**读、
    不是字节数——窗口一旦跨中文字符就会读偏/多读。用二进制模式按字节 seek/read，切完
    再统一 decode，避免半个字/窗口越界。"""
    with open(log_path, "rb") as f:
        f.seek(start_offset)
        chunk = f.read(max(0, end_offset - start_offset))
    return chunk.decode("utf-8", errors="replace")


def bind_cards(md_text: str, session_id: str) -> list:
    """绑卡：透传 everos_adapter.scan_terminal.session_case_entry_ids（spec §5 明确点名
    的既有函数，不重新实现）。"""
    return session_case_entry_ids(md_text, session_id)
