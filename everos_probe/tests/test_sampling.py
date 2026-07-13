import sqlite3

import msgpack
import pytest

from everos_probe import sampling


def _blob(d):
    return msgpack.packb(d, use_bin_type=True)


# ---------- 纯函数：tool_round_bucket / count_tool_rounds / normalize_source / stable_hash ----------

def test_tool_round_bucket_boundaries():
    assert sampling.tool_round_bucket(0) == "<3"
    assert sampling.tool_round_bucket(2) == "<3"
    assert sampling.tool_round_bucket(3) == "3-5"
    assert sampling.tool_round_bucket(5) == "3-5"
    assert sampling.tool_round_bucket(6) == "6+"
    assert sampling.tool_round_bucket(100) == "6+"


def test_count_tool_rounds_counts_paired_tool_call_rows():
    rows = [
        {"role": "user", "extra_bin": None, "extra_json": None},
        {"role": "tool_call", "extra_bin": _blob({"tool_call_id": "t1"}), "extra_json": None},
        {"role": "tool_result", "extra_bin": _blob({"tool_call_id": "t1"}), "extra_json": None},
        {"role": "tool_call", "extra_bin": _blob({"tool_call_id": "t2"}), "extra_json": None},
    ]
    assert sampling.count_tool_rounds(rows) == 2


def test_count_tool_rounds_ignores_unpaired_tool_call():
    """无 tool_call_id 的 tool_call 行在 role_map.py 里会降级为 synthetic assistant 文本，
    不是 EverOS 的 ToolCallRequest —— 不计入 round 数（对齐 everalgo
    `_count_tool_call_rounds` 只数真正的 ToolCallRequest）。"""
    rows = [{"role": "tool_call", "extra_bin": None, "extra_json": None}]
    assert sampling.count_tool_rounds(rows) == 0


def test_normalize_source_three_targets_and_openclaw_subagents():
    assert sampling.normalize_source("claude_code") == "claude_code"
    assert sampling.normalize_source("codex") == "codex"
    assert sampling.normalize_source("openclaw") == "openclaw"
    assert sampling.normalize_source("openclaw/main") == "openclaw"
    assert sampling.normalize_source("openclaw/wood") == "openclaw"


def test_normalize_source_rejects_non_target_agents():
    assert sampling.normalize_source("gemini") is None
    assert sampling.normalize_source("pi_agent") is None
    assert sampling.normalize_source("") is None
    assert sampling.normalize_source(None) is None


def test_stable_hash_is_deterministic_across_calls():
    assert sampling.stable_hash("eid-123") == sampling.stable_hash("eid-123")


def test_stable_hash_differs_for_different_ids():
    assert sampling.stable_hash("eid-1") != sampling.stable_hash("eid-2")


# ---------- has_pairable_extra ----------

def test_has_pairable_extra_true_when_any_tool_row_has_extra():
    rows = [{"role": "tool_call", "extra_bin": _blob({"tool_call_id": "t1"}), "extra_json": None}]
    assert sampling.has_pairable_extra(rows) is True


def test_has_pairable_extra_false_when_all_tool_rows_empty():
    rows = [
        {"role": "tool_call", "extra_bin": None, "extra_json": None},
        {"role": "tool_result", "extra_bin": None, "extra_json": "{}"},
    ]
    assert sampling.has_pairable_extra(rows) is False


def test_has_pairable_extra_true_when_no_tool_rows_at_all():
    # 无工具调用的真会话不算"数据损坏"——它会在 EverOS 结构门被正常拒，
    # 不是本函数要剔除的"adapter 拿不到配对 id"场景。
    rows = [{"role": "user", "extra_bin": None, "extra_json": None}]
    assert sampling.has_pairable_extra(rows) is True


# ---------- compute_quotas（手算校验，§4：floor + 按真实占比比例分配） ----------

def test_compute_quotas_floor_plus_proportional_remainder():
    sizes = {"claude_code|<3": 100, "claude_code|3-5": 50, "codex|6+": 10}
    quotas = sampling.compute_quotas(sizes, target_n=30, floor=5)
    assert quotas == {"claude_code|<3": 14, "claude_code|3-5": 10, "codex|6+": 6}
    assert sum(quotas.values()) == 30


def test_compute_quotas_floor_alone_can_exceed_target_n():
    sizes = {f"s{i}": 20 for i in range(7)}
    quotas = sampling.compute_quotas(sizes, target_n=30, floor=5)
    assert quotas == {f"s{i}": 5 for i in range(7)}
    assert sum(quotas.values()) == 35   # floor 优先，硬性下限,不因 target_n 更小而降


def test_compute_quotas_caps_at_stratum_population():
    sizes = {"a": 3, "b": 100}
    quotas = sampling.compute_quotas(sizes, target_n=20, floor=5)
    assert quotas == {"a": 3, "b": 17}
    assert sum(quotas.values()) == 20


def test_compute_quotas_never_exceeds_available_population_even_when_target_unreachable():
    sizes = {"a": 2, "b": 3}
    quotas = sampling.compute_quotas(sizes, target_n=20, floor=5)
    assert quotas == {"a": 2, "b": 3}


def test_compute_quotas_empty_strata_raises():
    with pytest.raises(ValueError):
        sampling.compute_quotas({}, target_n=10, floor=5)


def test_compute_quotas_no_remainder_loss_when_one_stratum_caps_out():
    """Controller 裁决 C2 锚定测试(a)：原「整数截断+stall_guard」实现在此输入上悄悄
    少发 2 个名额(54 而非 56)——s0 封顶(population=8)后,截断丢失的份额没有被重新分配
    给仍有余量的 s1。正确实现必须把 target_n 吃满(min(total_pop, target_n) == target_n
    此处为 56 <= total_pop=63)。"""
    quotas = sampling.compute_quotas({"s0": 8, "s1": 55}, target_n=56, floor=5)
    assert sum(quotas.values()) == 56
    assert quotas == {"s0": 8, "s1": 48}


def test_compute_quotas_nine_strata_long_tail_sums_to_min_population_target():
    """Controller 裁决 C2 锚定测试(b)：9 格(3 源 × 3 桶)长尾场景，其中一格总量
    (claude_code|3-5=6)略高于 floor=5、其余格总量远大于 target_n。总体 population
    (1094) 远超 target_n(90)，因此正确实现必须精确吃满 target_n，不允许因某格提前
    封顶而悄悄少发。"""
    sizes = {
        "claude_code|<3": 500, "claude_code|3-5": 6, "claude_code|6+": 40,
        "codex|<3": 300, "codex|3-5": 20, "codex|6+": 5,
        "openclaw|<3": 200, "openclaw|3-5": 15, "openclaw|6+": 8,
    }
    target_n = 90
    quotas = sampling.compute_quotas(sizes, target_n=target_n, floor=5)
    total_pop = sum(sizes.values())
    assert sum(quotas.values()) == min(total_pop, target_n)
    # 没有任何格超过自己的真实 population
    assert all(quotas[k] <= sizes[k] for k in sizes)


# ---------- select_sample（确定性 hash 选择） ----------

def test_select_sample_picks_lowest_hash_members_deterministically():
    members = [sampling.ConvMeta(i, f"eid-{i}", "a", 1, "<3", "a|<3") for i in range(10)]
    scan = sampling.LibraryScan({"a|<3": members}, 0, 0, 10)
    picked = sampling.select_sample(scan, {"a|<3": 3})
    expected = sorted(members, key=lambda m: sampling.stable_hash(m.external_id))[:3]
    assert [m.external_id for m in picked["a|<3"]] == [m.external_id for m in expected]
    picked2 = sampling.select_sample(scan, {"a|<3": 3})
    assert [m.external_id for m in picked2["a|<3"]] == [m.external_id for m in picked["a|<3"]]


# ---------- freeze_snapshot / load_snapshot round trip（bytes <-> JSON） ----------

def test_freeze_and_load_snapshot_round_trips_bytes(tmp_path):
    out = str(tmp_path / "snapshot.json")
    shares = {"a|<3": 1.0}
    meta = sampling.ConvMeta(1, "eid-1", "a", 1, "<3", "a|<3")
    selected = {"a|<3": [meta]}
    eb = _blob({"tool_call_id": "t1"})
    rows_by_conv = {"eid-1": [
        {"idx": 0, "role": "user", "content": "hi", "created_at": 1, "extra_bin": None, "extra_json": None},
        {"idx": 1, "role": "tool_call", "content": "Bash({})", "created_at": 2, "extra_bin": eb, "extra_json": None},
    ]}
    sampling.freeze_snapshot(out, shares, selected, rows_by_conv)
    manifest = sampling.load_snapshot(out)
    assert manifest["library_stratum_shares"] == shares
    loaded_rows = manifest["strata"]["a|<3"][0]["rows"]
    assert loaded_rows[0]["extra_bin"] is None
    assert loaded_rows[1]["extra_bin"] == eb
    assert manifest["strata"]["a|<3"][0]["external_id"] == "eid-1"


def test_freeze_snapshot_raises_on_duplicate_external_id_across_selected(tmp_path):
    """Controller 裁决③锚定测试:freeze_snapshot 用 external_id 作 rows_by_conv 的字典键,
    隐含假设选中会话的 external_id 互不相同。两个不同 ConvMeta(哪怕来自不同层)撞了
    同一个 external_id 时必须 fail-loud,而不是静默覆盖丢一条快照数据。"""
    out = str(tmp_path / "snapshot.json")
    shares = {"a|<3": 0.5, "b|<3": 0.5}
    m1 = sampling.ConvMeta(1, "eid-dup", "a", 1, "<3", "a|<3")
    m2 = sampling.ConvMeta(2, "eid-dup", "b", 1, "<3", "b|<3")
    selected = {"a|<3": [m1], "b|<3": [m2]}
    rows_by_conv = {"eid-dup": []}
    with pytest.raises(ValueError, match="eid-dup"):
        sampling.freeze_snapshot(out, shares, selected, rows_by_conv)


# ---------- stratum_shares 分母口径（Controller 裁决②：候选池占比，排除坏样本/无 eid） ----------

def test_stratum_shares_denominator_excludes_bad_and_missing_eid_from_candidate_pool():
    """Controller 裁决②锚定测试:wᵢ 的分母只对"已通过 has_pairable_extra 且有
    external_id 的候选池"求占比——排除 excluded_empty_extra(extra 全空坏样本)和
    skipped_no_external_id(无 external_id)的会话。这里直接构造一个 LibraryScan，
    excluded_empty_extra/skipped_no_external_id 计数很大，但它们根本不出现在
    scan.strata 里，验证 stratum_shares 的分母只覆盖候选池(与
    total_target_source_conversations 无关)。"""
    good = [sampling.ConvMeta(1, "eid-1", "claude_code", 1, "<3", "claude_code|<3")]
    scan = sampling.LibraryScan(
        strata={"claude_code|<3": good},
        excluded_empty_extra=3,
        skipped_no_external_id=5,
        total_target_source_conversations=9,  # 1(候选池) + 3(坏样本) + 5(无 eid)
    )
    shares = sampling.stratum_shares(scan)
    assert shares == {"claude_code|<3": 1.0}
    assert sum(shares.values()) == pytest.approx(1.0)


# ---------- 全量 sqlite fixture：scan_target_conversations / stratum_shares / run_sampling ----------

def _mk_db(path, convs):
    """convs: list of (id, agent_slug, external_id_or_None, msgs)
    msgs: list of (role, content, created_at, extra_bin, extra_json)"""
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE agents(id INTEGER PRIMARY KEY, slug TEXT);"
        "CREATE TABLE conversations(id INTEGER PRIMARY KEY, agent_id INT, external_id TEXT);"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INT, idx INT, role TEXT,"
        " content TEXT, created_at INT, extra_bin BLOB, extra_json TEXT);"
    )
    agent_ids = {}
    mid = 1
    for cid, slug, eid, msgs in convs:
        if slug not in agent_ids:
            agent_ids[slug] = len(agent_ids) + 1
            db.execute("INSERT INTO agents VALUES(?,?)", (agent_ids[slug], slug))
        db.execute("INSERT INTO conversations VALUES(?,?,?)", (cid, agent_ids[slug], eid))
        for i, (role, content, ts, eb, ej) in enumerate(msgs):
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
                       (mid, cid, i, role, content, ts, eb, ej))
            mid += 1
    db.commit()
    db.close()


def _tool_round(idx_ts, tcid):
    return [
        ("tool_call", "Bash({})", idx_ts, _blob({"tool_call_id": tcid, "tool_call_args": {}}), None),
        ("tool_result", "ok", idx_ts + 1, _blob({"tool_call_id": tcid}), None),
    ]


def _session(n_rounds, base_ts=1000):
    msgs = [("user", "hi", base_ts, None, None)]
    for k in range(n_rounds):
        msgs += _tool_round(base_ts + 10 + k * 10, f"t{k}")
    msgs.append(("assistant", "done", base_ts + 999, None, None))
    return msgs


def test_scan_target_conversations_builds_strata_and_excludes_bad_and_missing_eid(tmp_path):
    dbp = str(tmp_path / "c.db")
    convs = [
        (1, "claude_code", "eid-1", _session(1)),       # <3 桶
        (2, "claude_code", "eid-2", _session(4)),       # 3-5 桶
        (3, "codex", "eid-3", _session(7)),              # 6+ 桶
        (4, "codex", None, _session(4)),                 # 无 external_id -> 跳过
        (5, "openclaw/main", "eid-5", _session(2)),      # openclaw 归一化, <3 桶
        (6, "gemini", "eid-6", _session(2)),              # 非目标来源 -> 跳过（不计 total）
        (7, "claude_code", "eid-7", [
            ("user", "hi", 1, None, None),
            ("tool_call", "Bash({})", 2, None, None),     # 全空 extra -> 坏会话剔除
            ("tool_result", "o", 3, None, None),
            ("assistant", "done", 4, None, None),
        ]),
    ]
    _mk_db(dbp, convs)
    scan = sampling.scan_target_conversations(dbp)
    assert scan.total_target_source_conversations == 6   # 1,2,3,4,5,7（6 排除在外）
    assert scan.skipped_no_external_id == 1
    assert scan.excluded_empty_extra == 1
    assert set(scan.strata.keys()) == {"claude_code|<3", "claude_code|3-5", "codex|6+", "openclaw|<3"}
    assert all(len(v) == 1 for v in scan.strata.values())

    shares = sampling.stratum_shares(scan)
    assert shares == pytest.approx({"claude_code|<3": 0.25, "claude_code|3-5": 0.25,
                                     "codex|6+": 0.25, "openclaw|<3": 0.25})


def test_run_sampling_end_to_end_freezes_snapshot_feedable_by_pipeline(tmp_path):
    dbp = str(tmp_path / "c.db")
    out = str(tmp_path / "snapshot.json")
    convs = [
        (1, "claude_code", "eid-1", _session(1)),
        (2, "claude_code", "eid-2", _session(4)),
        (3, "codex", "eid-3", _session(7)),
        (4, "openclaw/main", "eid-4", _session(2)),
    ]
    _mk_db(dbp, convs)
    result = sampling.run_sampling(dbp, out, target_n=4, floor=1)
    assert result["out_path"] == out
    assert sum(result["quotas"].values()) == 4
    assert result["excluded_empty_extra"] == 0
    assert result["skipped_no_external_id"] == 0

    manifest = sampling.load_snapshot(out)
    all_eids = {m["external_id"] for members in manifest["strata"].values() for m in members}
    assert all_eids == {"eid-1", "eid-2", "eid-3", "eid-4"}

    # 冻结的行必须能直接喂进已上线的 everos_adapter.pipeline.run_session（不再碰 CASS）
    from everos_adapter.cap import NoopClamper
    from everos_adapter.pipeline import run_session
    import everos_adapter.feed as feed_mod

    seen = []

    def fake_post(url, json=None, timeout=None):
        if url.endswith("/add"):
            seen.append(json)
        class _R:
            def raise_for_status(self): pass
            def json(self): return {"request_id": "r", "data": {"status": "extracted"}}
        return _R()

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(feed_mod.httpx, "post", fake_post)
    try:
        one = manifest["strata"]["claude_code|3-5"][0]
        out_run = run_session("http://x", one["external_id"], one["rows"], "agent-x", "owner",
                               clamper=NoopClamper())
        assert out_run["skipped"] is False
        assert seen
    finally:
        mp.undo()
