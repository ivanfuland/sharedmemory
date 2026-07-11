"""infra/backup/cass/cass_backup_gate.py 的单元测试（腿 4：append-only 前缀全列摘要 +
单调性 + meta 水位，spec §5.5）。

覆盖 Task 6 brief 的全部测试要点：

Step 1（编码器单元测试，V5d3 全四条）：
  ① 值分隔符方案会碰撞（附录 A 探针自测①逐字复现），`_enc` 的长度前缀不会。
  ② header `'|'.join` 会碰撞（探针自测②），`prefix_digests` 的长度前缀 header 不会
     （用两张列名故意含 `|` 的空表验证，走真实实现而非重写逻辑）。
  ③ Tier A：合成库注入一条 `extra_bin` 含 `0x1F`/`0x1E`/`0x1D` 字节的行，断言
     `instr(...)>0` 且摘要在注入前后不同（Tier B 真实库探针属于 Task 18）。
  ④ 附录 A 探针脚本（`reference_digest_probe.py`，逐字抄自 spec）与生产实现
     `prefix_digests` 对同一合成库算出同一摘要。

Step 2（攻击夹具测试）：
  - 攻击②（清空 content）→ 前缀摘要不符，gap/单调性不受影响（V5）。
  - 攻击④（只改 author）→ 全列版抓到；劣化的「前 4 列」版对同一攻击失明（V5b）。
  - 攻击⑤（净缩尾）→ gap 仍 0，MAX(id)/COUNT 相对基线回退 ⇒ 单调性 FAIL（V5c）。
  - 删尾 N 再插 N（U12 不承诺项）→ 若替换发生在「上次备份 max_id 之后」的新增
    区间内，gap/单调性/前缀摘要三者全部放行（V5c2，可执行证明；若某天变 FAIL
    说明有人偷加了 B9 DB↔raw-mirror 交叉核对，需同步更新 spec）。
  - 攻击⑥（`last_scan_ts` 改小）→ 只有水位单调性拦，messages/conversations 的
    tables 结果不受影响（V5d）；`schema_version` 9→10 PASS、`value='abc'` FAIL（V5d2）。
  - 攻击⑦（删 `last_scan_ts` 整行）→ 必需水位键清单拦；一个「99% 阈值」式的
    行数校验本会放行（meta 9→8，8>=9*99//100=8），对照写在测试里（V5d4）。
  - 删中间一行（破坏连续性）→ gap 自检 FAIL，且 rebaseline 不豁免（V5f）。

外加：首晚登记 / 自比对 PASS / rebaseline 跳过 prev 比对但保留必需键与 gap 自检。
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import struct

import pytest

import fixture_factory
import reference_digest_probe
from cass_backup_gate import (
    REQUIRED_LEG4_WATERMARK_KEYS,
    TABLES_FOR_LEG4,
    Leg4Result,
    _enc,
    _leg4_parse_uint,
    leg4,
    prefix_digests,
)

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd 模板"
)


# ---------------------------------------------------------------------------
# Step 1 ①②：编码器单射性——值分隔符 / header 分隔符都会碰撞，长度前缀不会
# ---------------------------------------------------------------------------


def test_value_encoding_no_collision_v5d3_1():
    """spec §5.5 实测：`tag‖value‖0x1F` 拼接把 `['a\\x1ftb','c']` 与
    `['a','b\\x1ftc']` 序列化成同一串字节（附录 A 探针自测①逐字复现）；
    我们的长度前缀编码器 `_enc` 不会。"""

    def _bad_separator_encoding(values):
        out = b""
        for v in values:
            out += b"t" + v.encode() + b"\x1f"
        return out + b"\x1e"

    bad_a = _bad_separator_encoding(["a\x1ftb", "c"])
    bad_b = _bad_separator_encoding(["a", "b\x1ftc"])
    assert bad_a == bad_b, "反证：分隔符方案确实碰撞（spec 实测原文）"

    good_a = b"".join(_enc(v) for v in ["a\x1ftb", "c"])
    good_b = b"".join(_enc(v) for v in ["a", "b\x1ftc"])
    assert good_a != good_b, "长度前缀编码器（_enc）不应碰撞"


def test_header_encoding_no_collision_v5d3_2():
    """spec §5.5 实测：header 用 `'|'.join` 会把 `['a|b','c']` 与 `['a','b|c']`
    都序列化成 `"a|b|c"`（探针自测②）；`prefix_digests` 的长度前缀 header 不会
    ——用两张列名故意含 `|` 的空表跑真实实现验证。"""
    assert "|".join(["a|b", "c"]) == "|".join(["a", "b|c"]), "反证：naive join 确实碰撞"

    con = sqlite3.connect(":memory:")
    con.execute('CREATE TABLE t1 (id INTEGER PRIMARY KEY, "a|b" TEXT, "c" TEXT)')
    con.execute('CREATE TABLE t2 (id INTEGER PRIMARY KEY, "a" TEXT, "b|c" TEXT)')
    con.commit()

    _, digest_t1, _, _ = prefix_digests(con, "t1", None)
    _, digest_t2, _, _ = prefix_digests(con, "t2", None)
    con.close()

    assert digest_t1 != digest_t2, "长度前缀 header 不应把不同列名切分方式哈希成相同摘要"


# ---------------------------------------------------------------------------
# Step 1 ③：Tier A——合成库注入含 0x1F/0x1E/0x1D 字节的行，验证编码器真的处置了它
# ---------------------------------------------------------------------------


@requires_cass
def test_tier_a_injected_separator_bytes_change_digest_v5d3_3(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        _, digest_before, max_before, cnt_before = prefix_digests(con, "messages", None)
    finally:
        con.close()

    fixture_factory.inject_separator_bytes(db)

    con = sqlite3.connect(str(db))
    try:
        hits = con.execute(
            "SELECT SUM(instr(extra_bin, x'1f') > 0) FROM messages WHERE extra_bin IS NOT NULL"
        ).fetchone()[0]
        gap_cnt, gap_max = con.execute("SELECT COUNT(*), MAX(id) FROM messages").fetchone()
        _, digest_after, _, _ = prefix_digests(con, "messages", None)
    finally:
        con.close()

    assert hits > 0, "注入后应能验出含 0x1F 字节的行（Tier A：真实字节形态）"
    assert gap_cnt == gap_max == max_before + 1 == cnt_before + 1, (
        "MAX(id)+1 追加不应破坏 gap（id 连续）"
    )
    assert digest_before != digest_after, "注入含分隔符字节的行后，全列编码器摘要必须变化"


# ---------------------------------------------------------------------------
# Step 1 ④：附录 A 探针（原样抄）与生产实现对同一合成库算出同一摘要
# ---------------------------------------------------------------------------


@requires_cass
def test_reference_probe_matches_production_digest_v5d3_4(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        for table in TABLES_FOR_LEG4:
            max_id = con.execute(f'SELECT MAX(id) FROM "{table}"').fetchone()[0]
            _, production_digest, cur_max, _ = prefix_digests(con, table, None)
            assert cur_max == max_id

            reference_digest = reference_digest_probe.compute_digest(str(db), table, max_id)
            assert reference_digest == production_digest, (
                f'"{table}": 附录 A 探针与生产实现摘要不一致'
            )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Step 2：攻击②——清空 content 破坏前缀摘要，不影响 gap/单调性（V5）
# ---------------------------------------------------------------------------


@requires_cass
def test_attack2_content_cleared_breaks_prefix_digest_v5(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        baseline = leg4(con, prev_tables=None, prev_watermarks=None)
    finally:
        con.close()
    assert baseline.ok is True

    fixture_factory.attack2(db)

    con = sqlite3.connect(str(db))
    try:
        result = leg4(con, baseline.tables, baseline.meta_watermarks)
    finally:
        con.close()

    assert result.ok is False
    assert '"messages"' in result.detail
    assert "前缀摘要不符" in result.detail
    assert result.tables["messages"]["max_id"] == baseline.tables["messages"]["max_id"]
    assert result.tables["messages"]["count"] == baseline.tables["messages"]["count"]


# ---------------------------------------------------------------------------
# Step 2：攻击④——只改 author，全列版抓到；劣化的「前 4 列」版失明（V5b）
# ---------------------------------------------------------------------------


def _degraded_digest_first_n_cols(con, table, n):
    """劣化对照：只哈希前 n 列。messages 列序
    (id, conversation_id, idx, role, author, ...)——author 是第 5 列，取前 4 列
    (id, conversation_id, idx, role) 确保 author 确实不在内。

    = spec 附录 B「5 列版」历史对照的等价物，关键性质是**不含 author 列**
    （附录 B 那个「5 列版」哈希的是另一组选定列；本对照取前 4 列同样把 author
    排除在外，对攻击④失明的机理一致）。"""
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')][:n]
    h = hashlib.sha256()
    h.update(struct.pack(">Q", len(cols)))
    for c in cols:
        d = c.encode("utf-8")
        h.update(struct.pack(">Q", len(d)))
        h.update(d)
    for row in con.execute(f'SELECT * FROM "{table}" ORDER BY id'):
        truncated = row[:n]
        h.update(struct.pack(">Q", len(truncated)))
        for v in truncated:
            h.update(_enc(v))
    return h.hexdigest()


@requires_cass
def test_attack4_full_column_catches_author_change_but_degraded_4col_is_blind_v5b(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        cols = [r[1] for r in con.execute('PRAGMA table_info("messages")')]
        assert cols[:5] == ["id", "conversation_id", "idx", "role", "author"], (
            f"前置条件不成立，messages 列序意外: {cols}"
        )
        _, full_before, _, _ = prefix_digests(con, "messages", None)
        degraded_before = _degraded_digest_first_n_cols(con, "messages", 4)
    finally:
        con.close()

    fixture_factory.attack4(db)

    con = sqlite3.connect(str(db))
    try:
        _, full_after, _, _ = prefix_digests(con, "messages", None)
        degraded_after = _degraded_digest_first_n_cols(con, "messages", 4)
    finally:
        con.close()

    assert full_before != full_after, "全列版必须抓到只改 author 这一列"
    assert degraded_before == degraded_after, "前 4 列（不含 author）的劣化版必须对此攻击失明"


# ---------------------------------------------------------------------------
# Step 2：攻击⑤——小幅净缩尾，V5c 四断言逐条（gap / 幸存前缀 / 百分比阈值 / verdict）
# ---------------------------------------------------------------------------

_V5C_GROW_TO = 160  # 满库基线行数；删最后 1 行 ≈0.6%，贴近 spec 真实场景 1000/213195≈0.47%


def _grow_messages_to(db, target_rows):
    """把 messages 增长到 target_rows 行（id 连续追加，gap 保持 0），供 V5c 的
    小幅净缩尾构造使用——合成库原生只有 6 行，删 1 行占比太大（16.7%），撑不起
    「小幅删除 + 百分比阈值放行」的真实场景形态。"""
    con = sqlite3.connect(str(db))
    try:
        cur, first_conv = con.execute(
            "SELECT COUNT(*), (SELECT id FROM conversations ORDER BY id LIMIT 1) FROM messages"
        ).fetchone()
        for i in range(cur, target_rows):
            con.execute(
                "INSERT INTO messages (conversation_id, idx, role, author, created_at,"
                " content, extra_json, extra_bin) VALUES (?, ?, 'user', 'synth-grower',"
                " 0, ?, NULL, NULL)",
                (first_conv, i, f"filler-{i}"),
            )
        con.commit()
    finally:
        con.close()


@requires_cass
def test_attack5_small_tail_deletion_all_four_v5c_assertions(synth_dd):
    """V5c 四断言逐条（spec §5.5 攻击⑤，小幅净缩尾构造）：

    以满库（160 行）为 prev 基线，删**最后 1 行**（≈0.6%，贴近 spec 真实场景
    1000/213195≈0.47%）。逐条断言：
    ① gap 仍 0（`COUNT == MAX(id)`，净缩尾不产生空洞）；
    ② 幸存前缀摘要不变（夹具性格断言）：攻击前对 `id <= cur_max`（159）算的
       摘要 == 攻击后的全量摘要，逐字节相等——被删的只有尾行，幸存数据一个
       字节没动；
    ③ 百分比阈值放行的对照演示：`159 >= 160*99//100 = 158` 为 True——99% 行数
       阈值会放这个攻击过去（仿 V5d4 测试里 threshold_would_pass 的写法）；
    ④ 门 verdict：FAIL 且单调性判据命中（「回退」）。**同时**门的摘要比对也
       命中（「前缀摘要不符」）：cur_max 一旦跌破 prev 基线，流式哈希在更短的
       数据上无法复现「越过边界前」的内部状态——SHA-256 机理必然，与②不矛盾
       （②比的是「以 cur_max 为界」的幸存前缀性质，④比的是「以 prev.max_id
       为界」的基线前缀）。spec V5c 的「只有单调性能拦」是对 gap 自检 / 百分比
       阈值 / 幸存前缀性质这三类门说的——本测试用①②③证明那三类失明、用④
       证明单调性命中。
    """
    db = synth_dd / "agent_search.db"
    _grow_messages_to(db, _V5C_GROW_TO)

    con = sqlite3.connect(str(db))
    try:
        baseline = leg4(con, prev_tables=None, prev_watermarks=None)
        assert baseline.ok is True
        prev_max = baseline.tables["messages"]["max_id"]
        prev_count = baseline.tables["messages"]["count"]
        assert prev_max == prev_count == _V5C_GROW_TO, "前置条件：满库基线应为 160 行且 gap=0"
        # 攻击前留存「幸存前缀」（id <= 159）的摘要，供断言②比对。
        surviving_prefix_before, _, _, _ = prefix_digests(con, "messages", prev_max - 1)
    finally:
        con.close()

    fixture_factory.attack5(db, n_rows=1)

    con = sqlite3.connect(str(db))
    try:
        cur_max, cur_cnt = con.execute("SELECT MAX(id), COUNT(*) FROM messages").fetchone()
        _, full_digest_after, _, _ = prefix_digests(con, "messages", None)
        result = leg4(con, baseline.tables, baseline.meta_watermarks)
    finally:
        con.close()

    assert cur_max == cur_cnt == prev_max - 1, "前置条件：应恰好删掉最后 1 行"

    # ① gap 仍 0
    assert cur_cnt == cur_max, "gap 应仍为 0（净缩尾不产生空洞）"
    # ② 幸存前缀摘要不变（逐字节相等）
    assert full_digest_after == surviving_prefix_before, (
        "幸存前缀（id <= cur_max）的摘要必须逐字节等于攻击前同前缀的摘要"
    )
    # ③ 百分比阈值放行（对照演示）
    threshold_would_pass = cur_cnt >= prev_count * 99 // 100
    assert threshold_would_pass is True, "前置条件：99% 阈值本该放行这次删除才有对照意义"
    # ④ 门 verdict：单调性命中；基线前缀摘要比对同时命中（机理见 docstring）
    assert result.ok is False
    assert "回退" in result.detail
    assert "前缀摘要不符" in result.detail


# ---------------------------------------------------------------------------
# V5c2：删尾 N 再插 N（U12 不承诺项）——替换落在 prev 边界之后 ⇒ 全部放行（预期！）
# ---------------------------------------------------------------------------


@requires_cass
def test_delete_and_reinsert_same_count_passes_known_u12_gap(synth_dd):
    """U12（spec §5.5，non-goal）的可执行证明：`messages` 无 AUTOINCREMENT，
    rowid 会复用——删掉「上次备份 max_id 之后」新增区间的尾部 N 行、再插入 N 行
    全新内容，MAX(id)/COUNT 精确复原，且未被触碰的历史前缀（<= 基线 max_id）
    保持逐字节相等 ⇒ gap/单调性/前缀摘要三者全部放行。这是 spec 明确承诺不
    覆盖的盲区（真要拦需要 DB↔raw-mirror 交叉核对，backlog B9）。

    **若这个测试某天变 FAIL，说明有人偷加了 B9 交叉核对，需要同步更新 spec
    §1.4/§5.5 的 U12 措辞，不是本模块的回归。**
    """
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    new_growth = min(3, total - 1)
    assert new_growth >= 1, "前置条件：synth_dd 至少 2 条 messages 才能划出新增区"
    boundary = total - new_growth

    prev_digest, _, _, _ = prefix_digests(con, "messages", boundary)
    prev_messages = {
        "max_id": boundary,
        "count": con.execute(
            "SELECT COUNT(*) FROM messages WHERE id <= ?", (boundary,)
        ).fetchone()[0],
        "prefix_digest": prev_digest,
    }
    conv_max = con.execute("SELECT MAX(id) FROM conversations").fetchone()[0]
    conv_digest, _, _, _ = prefix_digests(con, "conversations", conv_max)
    prev_conversations = {
        "max_id": conv_max,
        "count": con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        "prefix_digest": conv_digest,
    }
    prev_watermarks = leg4(con, None, None).meta_watermarks
    first_conv = con.execute("SELECT id FROM conversations ORDER BY id LIMIT 1").fetchone()[0]
    con.close()

    # 攻击：删掉「新增区」尾部 new_growth 行，再插入 new_growth 行全新内容
    # （不指定 id，靠 rowid 复用精确复原 id——messages 无 AUTOINCREMENT）。
    con = sqlite3.connect(str(db))
    con.execute("DELETE FROM messages WHERE id > ?", (boundary,))
    for i in range(new_growth):
        con.execute(
            "INSERT INTO messages (conversation_id, idx, role, author, created_at,"
            " content, extra_json, extra_bin) VALUES (?, ?, 'user', 'attacker-reingest',"
            " 0, 'REPLACED-CONTENT', NULL, NULL)",
            (first_conv, 9000 + i),
        )
    con.commit()

    cur_max, cur_cnt = con.execute("SELECT MAX(id), COUNT(*) FROM messages").fetchone()
    assert cur_max == total and cur_cnt == total, (
        "前置条件不成立：rowid 复用应精确复原 MAX(id)/COUNT"
    )

    result = leg4(
        con,
        {"messages": prev_messages, "conversations": prev_conversations},
        prev_watermarks,
    )
    con.close()

    assert result.ok is True, f"U12 已知盲区应放行: {result.detail}"


# ---------------------------------------------------------------------------
# 攻击⑥：last_scan_ts 改小——只有水位单调性拦，messages/conversations 不受影响（V5d）
# ---------------------------------------------------------------------------


@requires_cass
def test_attack6_watermark_regression_caught_only_by_monotonicity_v5d(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        baseline = leg4(con, prev_tables=None, prev_watermarks=None)
    finally:
        con.close()
    assert baseline.ok is True

    fixture_factory.attack6(db)

    con = sqlite3.connect(str(db))
    try:
        result = leg4(con, baseline.tables, baseline.meta_watermarks)
    finally:
        con.close()

    assert result.ok is False
    assert "last_scan_ts" in result.detail
    assert "回退" in result.detail
    # 指纹/行数/摘要（messages/conversations 的 tables 部分）全不变：
    assert result.tables == baseline.tables
    assert '"messages"' not in result.detail
    assert '"conversations"' not in result.detail


# ---------------------------------------------------------------------------
# schema_version：9→10 数值比较 PASS；'abc' 解析失败 FAIL（V5d2）
# ---------------------------------------------------------------------------


@requires_cass
def test_schema_version_numeric_increase_passes_v5d2(synth_dd):
    """`meta.value` 存储类是 text——字符串比较 `"10">="9"` 为 False，必须按
    无符号整数比较才不会把 9→10 的合法迁移误判成回退。"""
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        baseline = leg4(con, prev_tables=None, prev_watermarks=None)
        con.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
        con.commit()
    finally:
        con.close()

    prev_watermarks = dict(baseline.meta_watermarks)
    prev_watermarks["schema_version"] = "9"

    con = sqlite3.connect(str(db))
    try:
        result = leg4(con, baseline.tables, prev_watermarks)
    finally:
        con.close()

    assert result.ok is True, f"schema_version 9→10 应 PASS: {result.detail}"


@requires_cass
def test_schema_version_non_numeric_value_fails_v5d2(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        baseline = leg4(con, prev_tables=None, prev_watermarks=None)
        con.execute("UPDATE meta SET value='abc' WHERE key='schema_version'")
        con.commit()
    finally:
        con.close()

    prev_watermarks = dict(baseline.meta_watermarks)
    prev_watermarks["schema_version"] = "9"

    con = sqlite3.connect(str(db))
    try:
        result = leg4(con, baseline.tables, prev_watermarks)
    finally:
        con.close()

    assert result.ok is False
    assert "schema_version" in result.detail
    assert "解析失败" in result.detail


@requires_cass
def test_watermark_trailing_newline_fails_full_leg4_v5d2_r7(synth_dd):
    """codex R7-P0 全腿层：`last_scan_ts` 改成「同数字 + 尾随 \\n」——旧 `.match()`
    会把它当合法整数放行（`int("...\\n")` 成功），fullmatch 后 leg4 必须 FAIL 且
    detail 指认水位解析失败。这是真库复现（probe-snapshot baseline + last_scan_ts
    改 `数字\\n` → gate rc=0）的单元层钉子。"""
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        baseline = leg4(con, prev_tables=None, prev_watermarks=None)
        cur_val = con.execute("SELECT value FROM meta WHERE key='last_scan_ts'").fetchone()[0]
        # 同数字 + 尾随换行：数值上「未回退」，只有整数门的严格性能拦它。
        con.execute("UPDATE meta SET value=? WHERE key='last_scan_ts'", (f"{cur_val}\n",))
        con.commit()
    finally:
        con.close()

    con = sqlite3.connect(str(db))
    try:
        result = leg4(con, baseline.tables, baseline.meta_watermarks)
    finally:
        con.close()

    assert result.ok is False, f"含尾随 \\n 的水位必须 FAIL: {result.detail}"
    assert "last_scan_ts" in result.detail
    assert "解析失败" in result.detail


# ---------------------------------------------------------------------------
# 攻击⑦：删 last_scan_ts 整行——必需清单拦，99% 阈值式检查本会放行（V5d4）
# ---------------------------------------------------------------------------


@requires_cass
def test_attack7_missing_watermark_key_caught_by_required_list_not_threshold_v5d4(synth_dd):
    """删掉 `meta` 里 `last_scan_ts` 整行：schema 指纹不变（不在本模块职责内，
    见 test_leg34_gate.py）、messages/conversations 摘要不变，一个「99% 阈值」
    式的行数校验会放行（meta 9→8 行，`8 >= 9*99//100 == 8`），但硬编码必需键
    清单会响亮 FAIL（spec §5.5(a)）。"""
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        baseline = leg4(con, prev_tables=None, prev_watermarks=None)
        prev_meta_count = con.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    finally:
        con.close()
    assert baseline.ok is True

    fixture_factory.attack7(db)

    con = sqlite3.connect(str(db))
    try:
        cur_meta_count = con.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
        result = leg4(con, baseline.tables, baseline.meta_watermarks)
    finally:
        con.close()

    # 阈值版对照（仅供演示反差，不是本模块的判据）：
    threshold_would_pass = cur_meta_count >= prev_meta_count * 99 // 100
    assert threshold_would_pass is True, "前置条件：99% 阈值本该放行这次删除才有对照意义"

    assert result.ok is False
    assert "last_scan_ts" in result.detail
    assert "必需水位键缺失" in result.detail
    assert result.tables == baseline.tables


# ---------------------------------------------------------------------------
# V5f：删中间一行——gap 自检 FAIL，rebaseline 不豁免
# ---------------------------------------------------------------------------


@requires_cass
def test_delete_middle_row_fails_gap_self_check_v5f(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    ids = [r[0] for r in con.execute("SELECT id FROM messages ORDER BY id").fetchall()]
    assert len(ids) >= 3, "前置条件：至少 3 条 messages 才能挖出一个真正的中间 id"
    middle_id = ids[len(ids) // 2]
    con.execute("DELETE FROM messages WHERE id=?", (middle_id,))
    con.commit()
    con.close()

    con = sqlite3.connect(str(db))
    try:
        result = leg4(con, prev_tables=None, prev_watermarks=None)
    finally:
        con.close()
    assert result.ok is False
    assert "gap 自检" in result.detail

    con = sqlite3.connect(str(db))
    try:
        result_rebaseline = leg4(con, prev_tables=None, prev_watermarks=None, rebaseline=True)
    finally:
        con.close()
    assert result_rebaseline.ok is False, "gap 自检必须在 rebaseline 下照跑"
    assert "gap 自检" in result_rebaseline.detail


# ---------------------------------------------------------------------------
# 首晚登记 / 自比对 PASS / rebaseline 跳过 prev 比对但保留必需键与 gap 自检
# ---------------------------------------------------------------------------


@requires_cass
def test_leg4_first_night_registers_without_baseline(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        result = leg4(con, prev_tables=None, prev_watermarks=None)
    finally:
        con.close()

    assert isinstance(result, Leg4Result)
    assert result.ok is True
    for table in TABLES_FOR_LEG4:
        assert table in result.tables
        assert result.tables[table]["count"] == result.tables[table]["max_id"]
    assert set(result.meta_watermarks) == set(REQUIRED_LEG4_WATERMARK_KEYS)


@requires_cass
def test_leg4_pass_when_compared_against_its_own_baseline(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        baseline = leg4(con, prev_tables=None, prev_watermarks=None)
        result = leg4(con, baseline.tables, baseline.meta_watermarks)
    finally:
        con.close()

    assert result.ok is True


@requires_cass
def test_leg4_rebaseline_skips_watermark_regression_but_keeps_required_keys(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    try:
        baseline = leg4(con, prev_tables=None, prev_watermarks=None)
    finally:
        con.close()

    fixture_factory.attack6(db)  # last_scan_ts 改小——rebaseline 应放行

    con = sqlite3.connect(str(db))
    try:
        result = leg4(con, baseline.tables, baseline.meta_watermarks, rebaseline=True)
    finally:
        con.close()
    assert result.ok is True, f"rebaseline 应放行水位回退: {result.detail}"

    fixture_factory.attack7(db)  # 再删掉 last_scan_ts 整行——必需清单永不可关

    con = sqlite3.connect(str(db))
    try:
        result2 = leg4(con, baseline.tables, baseline.meta_watermarks, rebaseline=True)
    finally:
        con.close()
    assert result2.ok is False, "rebaseline 不豁免必需水位键存在检查"
    assert "必需水位键缺失" in result2.detail


# ---------------------------------------------------------------------------
# 杂项：解析函数纯单元测试 + 必需键清单与 fixture_factory 交叉核对
# ---------------------------------------------------------------------------


def test_leg4_parse_uint_valid_and_invalid():
    assert _leg4_parse_uint("123") == 123
    assert _leg4_parse_uint("0") == 0
    assert _leg4_parse_uint("abc") is None
    assert _leg4_parse_uint("-1") is None
    assert _leg4_parse_uint(None) is None
    assert _leg4_parse_uint(123) is None


def test_leg4_parse_uint_rejects_dollar_anchor_edge_cases_v5d2():
    """codex R7-P0：`^[0-9]+$` + `.match()` 的 `$` 会匹配到 trailing newline **之前**，
    让 `"数字\\n"` 过匹配、`int()` 还能成功 → spec-invalid 水位当好备份放行。fullmatch
    整串锚定后，尾随 \\n / 前导 \\n / 内嵌空格 / 前后空白 一律 FAIL（返回 None）。"""
    assert _leg4_parse_uint("1783605600227\n") is None, "尾随 \\n 必须拒（P0 真库复现的核心）"
    assert _leg4_parse_uint("\n10") is None, "前导 \\n 必须拒"
    assert _leg4_parse_uint("10\n20") is None, "内嵌 \\n 必须拒"
    assert _leg4_parse_uint("1 0") is None, "内嵌空格必须拒"
    assert _leg4_parse_uint(" 10") is None, "前导空格必须拒"
    assert _leg4_parse_uint("10 ") is None, "尾随空格必须拒"
    assert _leg4_parse_uint("10\t") is None, "尾随制表符必须拒"
    assert _leg4_parse_uint("+10") is None, "前导正号必须拒"
    # 干净整数仍照常解析（不误伤）：
    assert _leg4_parse_uint("1783605600227") == 1783605600227


def test_required_watermark_keys_matches_fixture_factory_list():
    assert set(REQUIRED_LEG4_WATERMARK_KEYS) == set(fixture_factory.REQUIRED_META_KEYS)
