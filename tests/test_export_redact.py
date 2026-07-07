from cass_corpus import export as exp
from cass_corpus.pruner import Msg

_META = {"id": 7, "title": "fix auth", "agent": "codex", "started_at": 1700000000, "workspace": ""}

def _patch_reader(monkeypatch, meta, msgs):
    monkeypatch.setattr("cass_corpus.reader.get_conversation", lambda db, cid: meta)
    monkeypatch.setattr("cass_corpus.reader.max_message_ts", lambda db, cid: 1700000000)
    monkeypatch.setattr("cass_corpus.reader.read_messages", lambda db, cid: msgs)

def test_export_one_redacts_body_secret(tmp_path, monkeypatch):
    secret = "sk-ant-api03-" + "A" * 30
    _patch_reader(monkeypatch, dict(_META),
                  [Msg(idx=0, role="user", content=f"my key is {secret} ok")])
    rep = exp.export_one("ignored.db", str(tmp_path), 7, min_chars=1)
    assert rep["written"], "应写出文件"
    fn = rep["written"][0][0]
    content = (tmp_path / fn).read_text()
    assert secret not in content
    assert "[REDACTED_SECRET]" in content

def test_export_one_redacts_title_in_file_and_report(tmp_path, monkeypatch):
    secret = "ghp_" + "b" * 36
    meta = dict(_META); meta["id"] = 8; meta["title"] = f"token {secret}"
    _patch_reader(monkeypatch, meta,
                  [Msg(idx=0, role="user", content="hello world this is normal body content")])
    rep = exp.export_one("x.db", str(tmp_path), 8, min_chars=1)
    fn, _, title = rep["written"][0]
    assert secret not in title                      # report/stdout title 已脱敏
    assert secret not in (tmp_path / fn).read_text()  # frontmatter title 已脱敏（redact 覆盖全 text）

def test_export_batch_redacts_secret(tmp_path, monkeypatch):
    secret = "AKIA" + "1234567890ABCDEF"
    meta = dict(_META); meta["last_ts"] = 1700000000
    monkeypatch.setattr("cass_corpus.reader.select_conversations",
                        lambda *a, **k: [meta])
    monkeypatch.setattr("cass_corpus.reader.read_messages",
                        lambda db, cid: [Msg(idx=0, role="user", content=f"cred {secret} end")])
    rep = exp.export("ignored.db", str(tmp_path), limit=5, min_chars=1)
    assert rep["written"], "批量路径应写出文件"
    fn = rep["written"][0][0]
    assert secret not in (tmp_path / fn).read_text()
