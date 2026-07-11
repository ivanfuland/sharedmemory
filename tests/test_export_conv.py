# tests/test_export_conv.py
# export --conv <id> 单条精确导出（Inngest F3 逐条驱动用）。TDD：先失败。
# 全合成数据（PUBLIC 仓隐私）。messages 带 created_at（毫秒 epoch）以验 max_message_ts / exported_ts。
import os
import sqlite3
import threading

import pytest

from cass_corpus import export, render
from cass_corpus import export as _export


def _mk_db(path, convs):
    """convs: list of (id, agent_slug, last_ts, n_msgs, msg_len)。
    messages.created_at = last_ts - (n-1-i)*1000（i 越大越新），max = last_ts。"""
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE agents(id INTEGER PRIMARY KEY, slug TEXT);"
        "CREATE TABLE workspaces(id INTEGER PRIMARY KEY, path TEXT);"
        "CREATE TABLE conversations(id INTEGER PRIMARY KEY, title TEXT, workspace_id INT,"
        " source_path TEXT, started_at INT, last_message_created_at INT, agent_id INT, primary_model TEXT);"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INT, idx INT, role TEXT,"
        " content TEXT, created_at INT);"
    )
    mid = 1
    for cid, slug, last_ts, n, msg_len in convs:
        db.execute("INSERT OR IGNORE INTO agents VALUES(?,?)", (cid + 100, slug))
        db.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?)",
            (cid, f"t{cid}", None, None, last_ts, last_ts, cid + 100, "m"),
        )
        for i in range(n):
            body = f"turn {i} " + ("lorem ipsum dolor sit amet " * msg_len)
            created = last_ts - (n - 1 - i) * 1000  # 最后一条 = last_ts
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", (mid, cid, i, "user", body, created))
            mid += 1
    db.commit()
    db.close()


def test_export_one_writes_file_and_reports(tmp_path):
    p = str(tmp_path / "c.db")
    out = str(tmp_path / "out")
    _mk_db(p, [(1898, "claude_code", 1782896385426, 6, 120)])  # 长会话，稳过 min_chars
    rep = export.export_one(p, out, 1898)
    meta = {"id": 1898, "agent": "claude_code", "title": "t1898",
            "started_at": 1782896385426}
    fn = render.transcript_filename(meta)
    target = os.path.join(out, fn)
    assert os.path.exists(target), f"transcript not written: {target}"
    assert os.path.getsize(target) > 0
    assert rep["total"] == 1
    assert rep["skipped"] == [] and rep["errors"] == []
    assert len(rep["written"]) == 1 and rep["written"][0][0] == fn
    assert rep["exported_ts"] == 1782896385426  # = max(messages.created_at)


def test_export_one_missing_conv_returns_total_0(tmp_path):
    p = str(tmp_path / "c.db")
    out = str(tmp_path / "out")
    _mk_db(p, [(1898, "claude_code", 1782896385426, 6, 120)])
    rep = export.export_one(p, out, 999999)  # 不存在
    assert rep["total"] == 0
    assert rep["written"] == [] and rep["skipped"] == [] and rep["errors"] == []


def test_export_one_too_short_is_skipped(tmp_path):
    p = str(tmp_path / "c.db")
    out = str(tmp_path / "out")
    _mk_db(p, [(42, "claude_code", 1782896385426, 2, 1)])  # 极短 → 渲染 < min_chars
    rep = export.export_one(p, out, 42)
    assert rep["total"] == 1
    assert rep["written"] == []
    assert len(rep["skipped"]) == 1
    assert rep["exported_ts"] == 1782896385426  # 即便 skip 也记内容版本


# --- CLI arg parsing (codex 复审 P2 + adapter reviewer coverage gap) ---

def test_parse_argv_space_form():
    conv, pos, bf = export.parse_argv(["/out", "--conv", "1898"])
    assert conv == "1898" and pos == ["/out"] and bf is False


def test_parse_argv_equals_form():
    # codex 复审 P2：等号形必须识别（否则静默走批量 run_feed 推进水位线）
    conv, pos, bf = export.parse_argv(["/out", "--conv=1898"])
    assert conv == "1898" and pos == ["/out"]


def test_parse_argv_no_conv_is_batch():
    conv, pos, bf = export.parse_argv(["/out", "50", "--backfill"])
    assert conv is None and pos == ["/out", "50"] and bf is True


def test_parse_argv_trailing_conv_no_value():
    conv, pos, bf = export.parse_argv(["/out", "--conv"])  # 尾随无值 → None，不崩
    assert conv is None and pos == ["/out"]


def test_parse_argv_out_dir_equal_conv_value():
    # 按位置排除 --conv 值：out_dir 字符串恰等于 conv-id 也不被误吞
    conv, pos, bf = export.parse_argv(["1898", "--conv", "1898"])
    assert conv == "1898" and pos == ["1898"]


# ── 迁移守卫:拒绝把新命名刷进含旧 rowid 命名的目录（codex PR#41 P1）──

def test_export_refuses_legacy_named_corpus_dir(tmp_path, monkeypatch):
    """export 只写不删。直接刷进旧目录 → 新旧并存、gbrain 看到重复/孤儿。必须 fail loud。"""
    monkeypatch.delenv("CASS_CORPUS_ALLOW_MIXED", raising=False)
    out = tmp_path / "corpus"; out.mkdir()
    (out / "2026-04-29-cass-codex-192.md").write_text("legacy", encoding="utf-8")
    with pytest.raises(_export.LegacyCorpusDirError) as e:
        _export.export_one("/nonexistent.db", str(out), 1)
    assert "192" in str(e.value)


def test_export_ok_in_fresh_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("CASS_CORPUS_ALLOW_MIXED", raising=False)
    out = tmp_path / "fresh"
    _export._assert_no_legacy_names(str(out))           # 不存在的目录 → 放行
    out.mkdir(); (out / "2026-04-29-cass-codex-sdeadbeefdeadbeef.md").write_text("new", encoding="utf-8")
    _export._assert_no_legacy_names(str(out))           # 只有新命名 → 放行


def test_export_mixed_escape_hatch(tmp_path, monkeypatch):
    monkeypatch.setenv("CASS_CORPUS_ALLOW_MIXED", "1")
    out = tmp_path / "corpus"; out.mkdir()
    (out / "2026-04-29-cass-codex-192.md").write_text("legacy", encoding="utf-8")
    _export._assert_no_legacy_names(str(out))           # 显式放行,不抛


def test_collision_blocked_across_batches(tmp_path, monkeypatch):
    """codex R2 P1:批内 seen_names 挡不住跨轮。round1 写 conv1、游标推进;round2 从 conv2 起,
    旧实现无条件 os.replace 覆盖 conv1 且 errors=[] —— 静默丢一整个会话。"""
    monkeypatch.delenv("CASS_CORPUS_ALLOW_MIXED", raising=False)
    dbp = str(tmp_path / "c.db"); _mk_db_two_convs(dbp)
    out = str(tmp_path / "out")
    monkeypatch.setattr(render, "transcript_filename", lambda meta: "collide.md")
    r1 = _export.export(dbp, out, limit=1, min_turns=1, min_chars=1, since_cursor=(0, 0))
    assert len(r1["written"]) == 1 and "external_id: ext-a" in open(os.path.join(out, "collide.md")).read()
    r2 = _export.export(dbp, out, limit=1, min_turns=1, min_chars=1, since_cursor=r1["max_cursor"])
    assert r2["written"] == []                                   # 绝不覆盖
    assert len(r2["errors"]) == 1 and "拒绝覆盖" in r2["errors"][0][1]
    assert "external_id: ext-a" in open(os.path.join(out, "collide.md")).read()   # conv1 完好


def test_collision_blocked_in_export_one(tmp_path, monkeypatch):
    """F3 真实路径。旧实现这里连 seen_names 都没有,直接覆盖。"""
    monkeypatch.delenv("CASS_CORPUS_ALLOW_MIXED", raising=False)
    dbp = str(tmp_path / "c.db"); _mk_db_two_convs(dbp)
    out = str(tmp_path / "out")
    monkeypatch.setattr(render, "transcript_filename", lambda meta: "collide.md")
    assert len(_export.export_one(dbp, out, 1, min_chars=1)["written"]) == 1
    rep = _export.export_one(dbp, out, 2, min_chars=1)            # 同名、不同身份
    assert rep["written"] == [] and len(rep["errors"]) == 1
    assert "拒绝覆盖" in rep["errors"][0][1]
    assert "external_id: ext-a" in open(os.path.join(out, "collide.md")).read()


def test_true_hash_collision_detected_by_preimage(tmp_path, monkeypatch):
    """**真**碰撞:两个不同会话算出同一个 session_key(文件名与 frontmatter 的 key 都相同)。
    此时比 session_key 两边永远相等 —— 抓不到。必须比原像 (external_id, source_id, agent)。
    上面两条碰撞测试是 monkeypatch 文件名造的"假碰撞",不覆盖这条。"""
    monkeypatch.delenv("CASS_CORPUS_ALLOW_MIXED", raising=False)
    dbp = str(tmp_path / "c.db"); _mk_db_two_convs(dbp)
    out = str(tmp_path / "out")
    monkeypatch.setattr(render, "session_key", lambda meta: "scollide0000000")   # 强制碰撞
    assert len(_export.export_one(dbp, out, 1, min_chars=1)["written"]) == 1
    body = open(os.path.join(out, os.listdir(out)[0])).read()
    assert "session_key: scollide0000000" in body and "external_id: ext-a" in body
    rep = _export.export_one(dbp, out, 2, min_chars=1)      # 同 key、同名、不同 external_id
    assert rep["written"] == [] and len(rep["errors"]) == 1
    assert "拒绝覆盖" in rep["errors"][0][1]
    assert "external_id: ext-a" in open(os.path.join(out, os.listdir(out)[0])).read()


def test_same_session_reexport_overwrites_normally(tmp_path, monkeypatch):
    """守卫只拦"同名不同身份"。同一会话的内容更新必须照常覆盖,否则增量 feed 就死了。"""
    monkeypatch.delenv("CASS_CORPUS_ALLOW_MIXED", raising=False)
    dbp = str(tmp_path / "c.db"); _mk_db_two_convs(dbp)
    out = str(tmp_path / "out")
    assert len(_export.export_one(dbp, out, 1, min_chars=1)["written"]) == 1
    rep = _export.export_one(dbp, out, 1, min_chars=1)            # 再导同一条
    assert len(rep["written"]) == 1 and rep["errors"] == []


def test_guard_refuses_foreign_file(tmp_path, monkeypatch):
    """目标已存在但无 frontmatter(外来文件)→ 拒写,不当成自己的旧版覆盖掉。"""
    monkeypatch.delenv("CASS_CORPUS_ALLOW_MIXED", raising=False)
    p = tmp_path / "x.md"; p.write_text("not a transcript", encoding="utf-8")
    with pytest.raises(_export.TranscriptIdentityError):
        _export._guard_write_target(str(p), {"external_id": "e", "source_id": "local", "agent": "codex"})


def test_legacy_name_regex_does_not_hit_foreign_md(tmp_path, monkeypatch):
    """codex R2 P2:`-\d+\.md$` 会误伤任意 note-123.md。锚到 CASS 形态。"""
    monkeypatch.delenv("CASS_CORPUS_ALLOW_MIXED", raising=False)
    out = tmp_path / "d"; out.mkdir()
    (out / "note-123.md").write_text("x", encoding="utf-8")
    _export._assert_no_legacy_names(str(out))                    # 不该抛
    (out / "2026-04-29-cass-codex-192.md").write_text("x", encoding="utf-8")
    with pytest.raises(_export.LegacyCorpusDirError):
        _export._assert_no_legacy_names(str(out))


def _mk_db_two_convs(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE agents(id INTEGER PRIMARY KEY, slug TEXT);
        CREATE TABLE conversations(id INTEGER PRIMARY KEY, agent_id INTEGER, title TEXT,
            workspace_id INTEGER, source_path TEXT, started_at INTEGER,
            last_message_created_at INTEGER, primary_model TEXT,
            external_id TEXT, source_id TEXT);
        CREATE TABLE workspaces(id INTEGER PRIMARY KEY, path TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER, idx INTEGER,
            role TEXT, content TEXT, created_at INTEGER, extra_json TEXT, extra_bin BLOB);
        INSERT INTO agents VALUES(1,'codex');
        INSERT INTO conversations(id,agent_id,title,source_path,started_at,last_message_created_at,external_id,source_id)
          VALUES(1,1,'a','/p',1735660800000,1735660800000,'ext-a','local'),
                (2,1,'b','/p',1735660900000,1735660900000,'ext-b','local');
        INSERT INTO messages(conversation_id,idx,role,content,created_at)
          VALUES(1,0,'user','aaaa',1735660800000),(2,0,'user','bbbb',1735660900000);
    """)
    db.commit(); db.close()


def _mk_db_n_convs(path, n):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE agents(id INTEGER PRIMARY KEY, slug TEXT);
        CREATE TABLE conversations(id INTEGER PRIMARY KEY, agent_id INTEGER, title TEXT,
            workspace_id INTEGER, source_path TEXT, started_at INTEGER,
            last_message_created_at INTEGER, primary_model TEXT,
            external_id TEXT, source_id TEXT);
        CREATE TABLE workspaces(id INTEGER PRIMARY KEY, path TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER, idx INTEGER,
            role TEXT, content TEXT, created_at INTEGER, extra_json TEXT, extra_bin BLOB);
        INSERT INTO agents VALUES(1,'codex');
    """)
    for i in range(1, n + 1):
        db.execute("INSERT INTO conversations(id,agent_id,title,source_path,started_at,"
                   "last_message_created_at,external_id,source_id) VALUES(?,?,?,?,?,?,?,?)",
                   (i, 1, f"t{i}", "/p", 1735660800000 + i, 1735660800000 + i, f"ext-{i}", "local"))
        db.execute("INSERT INTO messages(conversation_id,idx,role,content,created_at) VALUES(?,?,?,?,?)",
                   (i, 0, "user", "x" * 50, 1735660800000 + i))
    db.commit(); db.close()


def test_parse_frontmatter_identity_ok():
    s = "---\nsource: cass\nexternal_id: ext-a\nsource_id: local\nagent: codex\n---\nbody\n"
    ident, dup, status = _export._parse_frontmatter_identity(s)
    assert status == "OK" and dup == set()
    assert ident == ("ext-a", "local", "codex")


def test_parse_frontmatter_identity_unclosed():
    ident, dup, status = _export._parse_frontmatter_identity("---\nexternal_id: ext-a\nno closing fence\n")
    assert status == "UNCLOSED"


def test_parse_frontmatter_identity_duplicate_key():
    s = "---\nexternal_id: ext-a\nexternal_id: evil\nsource_id: local\nagent: codex\n---\n"
    ident, dup, status = _export._parse_frontmatter_identity(s)
    assert status == "OK" and "external_id" in dup


def test_identity_of_file_long_title_not_frozen(tmp_path):
    """cap 充分性守卫（非 base-regression）：身份字段在顶部、title 排其后，故 base(4096) 也读得到真身份。
    本测试防将来有人把 _read_frontmatter_head 的 cap 改回 4096——那样 8000 字 title 会把闭合 --- 推到
    cap 之外，parse 得 UNCLOSED → _identity_of_file 返 FOREIGN → 二次导出永久冻结（codex R1 P1-1）。"""
    p = tmp_path / "long.md"
    body = ("---\nsource: cass\nexternal_id: ext-a\nsource_id: local\nagent: codex\n"
            "title: " + ("x" * 8000) + "\n---\n正文\n")
    p.write_text(body, encoding="utf-8")
    assert _export._identity_of_file(str(p)) == ("ext-a", "local", "codex")


def test_identity_of_file_duplicate_identity_is_foreign(tmp_path):
    """磁盘已有文件含重复身份 key（被注入污染）→ _identity_of_file 判 FOREIGN，
    guard 绝不信任它的 last-wins 身份（codex R1 P1-2）。"""
    p = tmp_path / "dup.md"
    p.write_text("---\nexternal_id: victim\nexternal_id: ext-a\nsource_id: local\nagent: codex\n---\n",
                 encoding="utf-8")
    assert _export._identity_of_file(str(p)) == "FOREIGN"


def test_identity_of_file_huge_unclosed_is_foreign(tmp_path):
    """超长单行 + 无闭合 fence（malformed/攻击文件）→ 有界 read(cap) 只读 ≤cap 字符、判 FOREIGN，
    不把整个巨行读进内存（codex R2 must-fix：`for line in f` 会）。"""
    p = tmp_path / "huge.md"
    p.write_text("---\n" + "x" * 300000, encoding="utf-8")     # 无闭合 ---，单行 >cap
    assert _export._identity_of_file(str(p)) == "FOREIGN"


def test_validate_text_identity_whitespace_canonical():
    """身份值前后空格：parser 的 .strip() 与 _identity_of_meta 的 strip 对称 → 不误 fail-loud（codex R2 P2#3）。"""
    meta = {"external_id": " ext-a ", "source_id": "local", "agent": "codex"}
    text = "---\nexternal_id: ext-a\nsource_id: local\nagent: codex\n---\nbody\n"
    assert _export._validate_text_identity(text, meta) is True


def test_validate_text_identity_match(tmp_path):
    meta = {"external_id": "ext-a", "source_id": "local", "agent": "codex"}
    text = "---\nsource: cass\nexternal_id: ext-a\nsource_id: local\nagent: codex\n---\nbody\n"
    assert _export._validate_text_identity(text, meta) is True


def test_validate_text_identity_mismatch_redacted():
    # 模拟 redact 把 external_id 改成 REDACTED
    meta = {"external_id": "ext-a", "source_id": "local", "agent": "codex"}
    text = "---\nexternal_id: [REDACTED_SECRET]\nsource_id: local\nagent: codex\n---\nbody\n"
    assert _export._validate_text_identity(text, meta) is False


def test_validate_text_identity_duplicate_injected():
    meta = {"external_id": "ext-a", "source_id": "local", "agent": "codex"}
    text = "---\nexternal_id: ext-a\nsource_id: local\nagent: codex\nexternal_id: evil\n---\n"
    assert _export._validate_text_identity(text, meta) is False


def test_validate_text_identity_legacy_schema():
    # legacy meta 无 external_id/source_id → 身份 (None, None, agent)，text 也不写这两行
    meta = {"agent": "codex"}
    text = "---\nsource: cass\nsession_key: sabc\nagent: codex\n---\nbody\n"
    assert _export._validate_text_identity(text, meta) is True


def test_validate_text_identity_malformed_no_frontmatter():
    meta = {"external_id": "ext-a", "source_id": "local", "agent": "codex"}
    assert _export._validate_text_identity("just text\nexternal_id: ext-a\n", meta) is False


# ── Task 3: os.link 原子占位熔合 + guard lexists + tmp uuid ──

def test_write_transcript_refuses_identity_mismatch(tmp_path):
    """写入层自校验：待写 text 身份 ≠ meta 身份 → raise 且不落盘（抓"摘掉写前自校验"变异）。
    直接测 _write_transcript（谓词级测试挡不住这个变异——它们不经过写路径）。"""
    p = str(tmp_path / "x.md")
    meta = {"external_id": "ext-a", "source_id": "local", "agent": "codex"}
    bad = "---\nexternal_id: [REDACTED_SECRET]\nsource_id: local\nagent: codex\n---\nbody\n"
    with pytest.raises(_export.TranscriptIdentityError):
        _export._write_transcript(p, bad, meta)
    assert not os.path.exists(p)                                # 绝不落盘


def test_reexport_same_session_updates_content(tmp_path, monkeypatch):
    """同身份再导走 EEXIST → os.replace，且**内容真更新**（不只 written/errors 绿 —— codex R1 P2-1：
    内容不变也会绿是弱断言）。第二次前改 DB 消息内容，断言磁盘文件内容随之变化。"""
    monkeypatch.delenv("CASS_CORPUS_ALLOW_MIXED", raising=False)
    dbp = str(tmp_path / "c.db"); _mk_db_two_convs(dbp)
    out = str(tmp_path / "out")
    assert len(_export.export_one(dbp, out, 1, min_chars=1)["written"]) == 1
    fn = os.path.join(out, os.listdir(out)[0]); before = open(fn).read()
    db = sqlite3.connect(dbp)                                    # 改 conv1 的消息内容
    db.execute("UPDATE messages SET content='UPDATED_BODY_ZZZ' WHERE conversation_id=1"); db.commit(); db.close()
    rep = _export.export_one(dbp, out, 1, min_chars=1)           # EEXIST → 同身份 → os.replace
    assert len(rep["written"]) == 1 and rep["errors"] == []
    after = open(fn).read()
    assert "UPDATED_BODY_ZZZ" in after and after != before       # 内容确实被更新


def test_reexport_long_title_not_frozen(tmp_path, monkeypatch):
    """长 title（闭合 --- 超 4096）的会话二次导出不被冻结（codex R1 P1-1 端到端）。"""
    monkeypatch.delenv("CASS_CORPUS_ALLOW_MIXED", raising=False)
    dbp = str(tmp_path / "c.db"); _mk_db_two_convs(dbp)
    db = sqlite3.connect(dbp)
    db.execute("UPDATE conversations SET title=? WHERE id=1", ("x" * 8000,)); db.commit(); db.close()
    out = str(tmp_path / "out")
    assert len(_export.export_one(dbp, out, 1, min_chars=1)["written"]) == 1
    rep = _export.export_one(dbp, out, 1, min_chars=1)           # 不得因长 title 判 FOREIGN 而 拒绝覆盖
    assert len(rep["written"]) == 1 and rep["errors"] == []


def test_write_transcript_link_oserror_cleans_tmp(tmp_path, monkeypatch):
    """os.link 抛非-EEXIST OSError（如跨 fs EXDEV）→ 传播（上层进 errors），且 finally 清 tmp（codex R1 P2-2）。"""
    import errno as _errno
    p = str(tmp_path / "x.md")
    meta = {"external_id": "ext-a", "source_id": "local", "agent": "codex"}
    text = "---\nsource: cass\nexternal_id: ext-a\nsource_id: local\nagent: codex\n---\nbody\n"
    monkeypatch.setattr(_export.os, "link",
                        lambda s, d: (_ for _ in ()).throw(OSError(_errno.EXDEV, "cross-device")))
    with pytest.raises(OSError):
        _export._write_transcript(p, text, meta)
    leftovers = [f for f in os.listdir(tmp_path) if ".tmp." in f]
    assert leftovers == [] and not os.path.exists(p)            # tmp 已清、path 未建


def test_guard_refuses_symlink(tmp_path):
    target = tmp_path / "real.md"; target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.md"; link.symlink_to(target)
    with pytest.raises(_export.TranscriptIdentityError):
        _export._guard_write_target(str(link), {"external_id": "e", "source_id": "local", "agent": "codex"})
    dangling = tmp_path / "dangling.md"; dangling.symlink_to(tmp_path / "nope.md")
    with pytest.raises(_export.TranscriptIdentityError):
        _export._guard_write_target(str(dangling), {"external_id": "e", "source_id": "local", "agent": "codex"})


def test_concurrent_collision_exactly_one_survives(tmp_path, monkeypatch):
    """K 个不同身份会话被 monkeypatch 成同文件名（真碰撞代理）→ 恰一个存活、K-1 报 拒绝覆盖、无静默覆盖。
    对齐到 os.link 并断言其被命中：'退回旧两步' mutant 不调 os.link → 命中数 0 → 本测必红（防假绿）。"""
    monkeypatch.delenv("CASS_CORPUS_ALLOW_MIXED", raising=False)
    K = 8
    dbp = str(tmp_path / "c.db"); _mk_db_n_convs(dbp, K)
    out = str(tmp_path / "out")
    monkeypatch.setattr(render, "transcript_filename", lambda meta: "collide.md")

    real_link = os.link
    hits = {"n": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(K, timeout=30)

    def counting_link(src, dst):
        with lock:
            hits["n"] += 1
        barrier.wait()                      # 全部 K 线程对齐到 link 点，最大化重叠
        return real_link(src, dst)
    monkeypatch.setattr(_export.os, "link", counting_link)

    reps = [None] * K

    def worker(i):
        reps[i] = _export.export_one(dbp, out, i + 1, min_chars=1)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(K)]
    for t in threads: t.start()
    for t in threads: t.join()

    written = sum(len(r["written"]) for r in reps)
    errored = [e for r in reps for e in r["errors"]]
    assert written == 1, f"应恰好一个存活，实际 {written}"
    assert len(errored) == K - 1 and all("拒绝覆盖" in e[1] for e in errored)
    assert hits["n"] >= K, "os.link 未被命中：熔合被绕过（防假绿断言）"
    survivors = os.listdir(out)
    assert survivors == ["collide.md"]
    body = open(os.path.join(out, "collide.md")).read()
    assert sum(1 for ln in body.splitlines() if ln.startswith("external_id:")) == 1   # 身份自洽、无叠写
