"""tests/backup/fixture_factory.py 的自验测试。

覆盖：
  - Step 1: make_session_jsonl 生成的合成会话能被真 cass claude connector 摄入
  - Step 2: build_data_dir 自建的 data_dir 有完整 schema / raw-mirror / 必需水位键
  - Step 3: 攻击构造①–⑦ 各自的「构造已生效」断言
  - Step 4: 合成 data_dir 上真跑 cass doctor 的 manifest_blake3 兼容性探证

全部依赖真 cass 二进制（marker realcass + skipif 缺失时跳过，不是硬 fail——
cass 是外部已装二进制，不归本仓管，缺失时应跳过而非阻塞其它测试）。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess

import pytest

import fixture_factory

pytestmark = [
    pytest.mark.realcass,
    pytest.mark.skipif(shutil.which("cass") is None, reason="需要真 cass 二进制"),
]


def _last_json_doc(stdout: str) -> dict:
    """`cass ... --json` 的 stdout 是一串 NDJSON 进度事件（started/phase/phase/...）
    再加末尾一份 pretty-printed 的最终摘要——不是单个 JSON 值，`json.loads` 整体解析
    会因「Extra data」报错。用 `raw_decode` 顺序剥离全部内嵌对象，取最后一个
    （即最终摘要，顶层含 `conversations`/`messages` 总数）。"""
    decoder = json.JSONDecoder()
    idx = 0
    last = None
    while idx < len(stdout):
        remainder = stdout[idx:]
        stripped = remainder.lstrip()
        idx += len(remainder) - len(stripped)
        if idx >= len(stdout):
            break
        obj, end = decoder.raw_decode(stdout, idx)
        last = obj
        idx = end
    assert last is not None, f"cass --json 输出不含任何 JSON 对象:\n{stdout}"
    return last


# ---------------------------------------------------------------------------
# Step 1: make_session_jsonl 的可摄入性
# ---------------------------------------------------------------------------


def test_make_session_jsonl_is_ingestible_by_real_cass(tmp_home):
    session_path = tmp_home / ".claude" / "projects" / "testproj" / "session1.jsonl"
    fixture_factory.make_session_jsonl(session_path, n_msgs=6, salt="t1")

    result = subprocess.run(
        ["cass", "index", "--json"],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_home)},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"cass index 失败:\n{result.stdout}\n{result.stderr}"
    report = _last_json_doc(result.stdout)
    assert report["conversations"] >= 1, "合成会话未被摄入任何 conversation"
    assert report["messages"] >= 3, "合成会话摄入的消息数不足 3"


def test_make_session_jsonl_uses_nonsense_content_only(tmp_path):
    """PUBLIC 仓纪律的可执行断言：文本必须是 lorem-{salt}-{i} 形式，不含真实内容。"""
    session_path = tmp_path / "session.jsonl"
    fixture_factory.make_session_jsonl(session_path, n_msgs=4, salt="xyz")

    for line in session_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        content = record.get("message", {}).get("content")
        if content is not None:
            assert content.startswith("lorem-xyz-"), f"合成内容不是 nonsense: {content!r}"


# ---------------------------------------------------------------------------
# Step 2: build_data_dir 的产物完整性
# ---------------------------------------------------------------------------


def test_build_data_dir_produces_full_schema_raw_mirror_and_watermarks(tmp_path):
    home = tmp_path / "home"
    data_dir = fixture_factory.build_data_dir(home)

    db_path = data_dir / "agent_search.db"
    assert db_path.is_file(), "build_data_dir 未产出 agent_search.db"

    con = sqlite3.connect(str(db_path))
    try:
        table_count = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        # 真 cass schema 23 张 + build_data_dir 补造的 legacy fts_messages FTS5 表
        # 及其 5 张 shadow 表（fts_messages_config/_content/_data/_docsize/_idx）= 29。
        assert table_count == 29, f"真 cass schema + 合成 FTS5 表应有 29 张表，实测 {table_count}"

        sources_count = con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        assert sources_count == 2, (
            f"腿 3 §5.4 part 2「2→1 丢一半」测试需要 sources ≥ 2 行，实测 {sources_count}"
        )

        keys = {row[0] for row in con.execute("SELECT key FROM meta")}
        missing = [k for k in fixture_factory.REQUIRED_META_KEYS if k not in keys]
        assert not missing, f"缺少 spec §5.5(a) 必需水位键: {missing}"
    finally:
        con.close()

    manifests = list((data_dir / "raw-mirror" / "v1" / "manifests").glob("*.json"))
    assert manifests, "raw-mirror manifests 为空"
    blobs = list((data_dir / "raw-mirror" / "v1" / "blobs").rglob("*.raw"))
    assert blobs, "raw-mirror blobs 为空"


# ---------------------------------------------------------------------------
# Step 3: 攻击构造①–⑦ 落地断言
# ---------------------------------------------------------------------------


def test_attack1_deletes_meta_table_from_schema(synth_dd):
    db = synth_dd / "agent_search.db"

    fixture_factory.attack1(db)

    con = sqlite3.connect(str(db))
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table: meta"):
            con.execute("SELECT COUNT(*) FROM meta")
    finally:
        con.close()

    # schema 里的 autoindex 也必须一并消失，否则 orphan index 会让 schema 解析不了（spec 附录 A 原话）
    con = sqlite3.connect(str(db))
    try:
        leftover = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('meta', 'sqlite_autoindex_meta_1')"
        ).fetchone()[0]
        assert leftover == 0
    finally:
        con.close()


def test_attack2_clears_message_content(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    con.close()
    assert total > 0, "synth data_dir 的 messages 表意外为空——攻击②的前置条件不成立"

    fixture_factory.attack2(db)

    con = sqlite3.connect(str(db))
    try:
        cleared = con.execute("SELECT SUM(content='') FROM messages").fetchone()[0]
        # spec 原话是「清 1000 条」；LIMIT 1000 在小于 1000 行的合成库上天然退化为「清空全部」
        assert cleared == min(total, 1000)
    finally:
        con.close()


def test_attack3_empties_agents_table_without_touching_schema(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    before = con.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    con.close()
    assert before > 0, "synth data_dir 的 agents 表意外为空——攻击③的前置条件不成立"

    fixture_factory.attack3(db)

    con = sqlite3.connect(str(db))
    try:
        after = con.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        assert after == 0
        schema_intact = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='agents'"
        ).fetchone()[0]
        assert schema_intact == 1, "攻击③不应动 schema，只清行"
    finally:
        con.close()


def test_attack4_only_changes_author_column(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    before_content = [row[0] for row in con.execute("SELECT content FROM messages ORDER BY id")]
    before_null_authors = con.execute(
        "SELECT COUNT(*) FROM messages WHERE author IS NULL"
    ).fetchone()[0]
    con.close()
    assert before_null_authors == len(before_content) > 0, (
        "synth 消息的 author 预期全为 NULL（连接器未写 model 字段）——攻击④的前置条件不成立"
    )

    fixture_factory.attack4(db)

    con = sqlite3.connect(str(db))
    try:
        after_content = [row[0] for row in con.execute("SELECT content FROM messages ORDER BY id")]
        assert after_content == before_content, "攻击④只应改 author 列，content 不能变"
        non_null_authors = con.execute(
            "SELECT COUNT(*) FROM messages WHERE author IS NOT NULL"
        ).fetchone()[0]
        assert non_null_authors == len(before_content), "攻击④应把 author 列全改掉"
    finally:
        con.close()


def test_attack5_shrinks_tail_keeping_gap_zero(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    before_count, before_max = con.execute("SELECT COUNT(*), MAX(id) FROM messages").fetchone()
    con.close()
    assert before_count >= 3, "synth messages 太少，无法验证按比例删尾"

    fixture_factory.attack5(db)

    con = sqlite3.connect(str(db))
    try:
        after_count, after_max = con.execute("SELECT COUNT(*), MAX(id) FROM messages").fetchone()
        expected_deleted = before_count // 3
        assert expected_deleted > 0, "测试夹具消息数不足以产生非零删除量"
        assert after_count == before_count - expected_deleted
        assert after_max == before_max - expected_deleted
        # 净缩尾攻击的核心特征：gap 仍为 0——这正是 spec 里「只有单调性能拦」的原因
        assert after_max - after_count == 0
    finally:
        con.close()


def test_attack6_decreases_last_scan_ts_watermark(synth_dd):
    db = synth_dd / "agent_search.db"
    con = sqlite3.connect(str(db))
    before = con.execute("SELECT value FROM meta WHERE key='last_scan_ts'").fetchone()[0]
    con.close()

    fixture_factory.attack6(db)

    con = sqlite3.connect(str(db))
    try:
        after = con.execute("SELECT value FROM meta WHERE key='last_scan_ts'").fetchone()[0]
        assert after == "1"
        assert int(after) < int(before), "攻击⑥必须让水位变小（回退），否则不是这个攻击构造"
    finally:
        con.close()


def test_attack7_deletes_last_scan_ts_row_entirely(synth_dd):
    db = synth_dd / "agent_search.db"

    fixture_factory.attack7(db)

    con = sqlite3.connect(str(db))
    try:
        count = con.execute(
            "SELECT COUNT(*) FROM meta WHERE key='last_scan_ts'"
        ).fetchone()[0]
        assert count == 0
        # schema 指纹不受影响：meta 表本身还在
        schema_intact = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()[0]
        assert schema_intact == 1
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Step 4: manifest_blake3 兼容性探证 —— 真 doctor 对合成 data_dir 秒级返回
# ---------------------------------------------------------------------------


def test_synth_dd_doctor_clean(tmp_home, synth_dd):
    result = subprocess.run(
        ["cass", "doctor", "--json", "--data-dir", str(synth_dd)],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_home)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    # 永远不看 doctor 的退出码——exit=5 是已知假阳性（spec 附录 A）。解析 JSON 才是真判据。
    report = json.loads(result.stdout)
    raw_mirror = report["raw_mirror"]
    assert raw_mirror["status"] == "verified"
    assert raw_mirror["summary"]["manifest_checksum_mismatch_count"] == 0
    assert raw_mirror["summary"]["verified_blob_count"] > 0
