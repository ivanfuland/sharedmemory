from everos_probe import attribution, stats


def test_classify_log_window_detects_step1_empty_memcell():
    log = "INFO no items on memcell (n_items=0), skipping"
    assert attribution.classify_log_window(log) == attribution.STRUCTURAL_REJECT


def test_classify_log_window_detects_step3_should_skip():
    log = 'INFO skipping memcell (n_items=4): "No user messages found"'
    assert attribution.classify_log_window(log) == attribution.STRUCTURAL_REJECT


def test_classify_log_window_detects_step3b_min_rounds():
    log = "INFO skipping memcell — only 1 tool-call rounds < min 3"
    assert attribution.classify_log_window(log) == attribution.STRUCTURAL_REJECT


def test_classify_log_window_detects_semantic_filter():
    log = "INFO filtered out by LLM: Straightforward march, no detours"
    assert attribution.classify_log_window(log) == attribution.SEMANTIC_REJECT


def test_classify_log_window_detects_agent_case_skipped_no_assistant():
    # everos.memory.strategies.extract_agent_case：memcell 里没有任何 assistant
    # 发言人，structlog event（真跑经 ConsoleRenderer 渲染后仍是可搜的独立子串，
    # 前后 ANSI 着色不影响 .search()）。语义上是结构性拒 -> structural_reject。
    log = (
        "2026-07-13T11:34:34.009496Z [warning  ] agent_case_skipped_no_assistant "
        "memcell_id=mc_test_1 session_id=sess_test_1"
    )
    assert attribution.classify_log_window(log) == attribution.STRUCTURAL_REJECT


def test_classify_log_window_detects_empty_task_intent():
    # everalgo.agent_memory.case._compress_experience 第 626 行：LLM 抽取阶段判定
    # 无有效任务意图而跳过 -> 语义门拒。
    log = "INFO LLM returned empty 'task_intent', skipping"
    assert attribution.classify_log_window(log) == attribution.SEMANTIC_REJECT


def test_classify_log_window_detects_empty_approach():
    # 同上第 629 行，approach 分支（warning 级）。
    log = "WARNING LLM returned empty 'approach', skipping"
    assert attribution.classify_log_window(log) == attribution.SEMANTIC_REJECT


def test_classify_log_window_no_signal_returns_empty():
    log = "INFO memcell pre-trim total_tokens=500, message_count=10"
    assert attribution.classify_log_window(log) == ""


def test_classify_log_window_structural_wins_when_both_signals_present():
    # Controller 关注点 #3（pre-flight Task3-2）：brief 注释承认"理论上不该同时出现
    # 两种信号"，但仲裁规则本身此前没有测试覆盖。这里人为构造一个同时命中结构门
    # （Step3b：em dash + min rounds）和语义门（filtered out by LLM）的日志窗口，
    # 断言最终判定走结构拒——验证"结构优先于语义"的仲裁规则真的生效，不是巧合。
    log = (
        "INFO skipping memcell — only 1 tool-call rounds < min 3\n"
        "INFO filtered out by LLM: also flagged as low-signal"
    )
    assert attribution.classify_log_window(log) == attribution.STRUCTURAL_REJECT


def test_classify_session_pass_wins_even_with_log_noise():
    # 理论上过门会话不该同时出现拒绝日志，仍验证 pass 判据优先于日志信号
    log = "INFO filtered out by LLM: whatever"
    assert attribution.classify_session(log, ["ac_20260713_00000001"]) == attribution.PASS


def test_classify_session_structural_reject_when_no_cards():
    log = "INFO skipping memcell — only 1 tool-call rounds < min 3"
    assert attribution.classify_session(log, []) == attribution.STRUCTURAL_REJECT


def test_classify_session_semantic_reject_when_no_cards():
    log = "INFO filtered out by LLM: no detours"
    assert attribution.classify_session(log, []) == attribution.SEMANTIC_REJECT


def test_classify_session_other_when_no_signal_and_no_cards():
    assert attribution.classify_session("INFO memcell pre-trim total_tokens=1", []) == attribution.OTHER


def test_bind_cards_delegates_to_scan_terminal():
    md = (
        "<!-- entry:ac_20260713_00000001 -->\n"
        "## ac_20260713_00000001\n\n"
        "**owner_id**: agent-x\n"
        "**session_id**: demo-sess-01\n"
        "<!-- /entry:ac_20260713_00000001 -->\n"
    )
    assert attribution.bind_cards(md, "demo-sess-01") == ["ac_20260713_00000001"]
    assert attribution.bind_cards(md, "other-session") == []


def test_read_log_window_reads_byte_range(tmp_path):
    # 中文（EverOS 的 reason/会话摘要日志常含中文）在 UTF-8 里是变长多字节编码——用
    # 纯 ASCII fixture 测不出"按字符 read 而非按字节 read"这类 bug（旧实现文本模式
    # open + read(n) 按字符数读，字节窗口对上多字节内容时会跑偏/多读）。
    p = tmp_path / "server.log"
    content = "AAAAA你好BBBBB"
    p.write_bytes(content.encode("utf-8"))
    # "你好"各占 3 字节(UTF-8)：字节偏移 [5, 11) 恰好是这两个字。旧的文本模式实现在此
    # 会 seek 到字节 5 后 read(6) 读 6 个"字符"（你好BBBB），而非 6 个字节（你好），
    # 在此 fixture 上会失配——这就是本用例要抓的 bug。
    assert attribution.read_log_window(str(p), 5, 11) == "你好"


def test_fed_statuses_literal_consistency_with_stats():
    # Controller 关注点 #1：Task2 遗留的跨-task 待确认项。attribution.classify_session
    # 的四个返回值字面量必须与 everos_probe.stats.FED_STATUSES 逐字一致——两边各自独立
    # 定义、互不 import 生产代码（避免跨模块耦合），靠这条测试兜底：未来任一方改字符串
    # 拼写会立刻在这里变红，而不是悄悄在 aggregate_fed_outcomes 里 fail-loud 崩给用户看。
    assert {
        attribution.PASS,
        attribution.STRUCTURAL_REJECT,
        attribution.SEMANTIC_REJECT,
        attribution.OTHER,
    } == stats.FED_STATUSES
