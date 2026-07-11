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


from cass_corpus.redact import redact_transcript


def test_redact_transcript_preserves_identity():
    # external_id 命中 secret 正则（ak-前缀）—— 必须逐字保留，否则守卫误 raise / backfill orphan
    text = ("---\nsource: cass\nexternal_id: ak-ABCDEFGHIJKLMNOPQRSTUVWX\n"
            "source_id: local\nagent: codex\n---\n正文里 token=SECRETVALUE1234567890ABCD 应被脱敏\n")
    out = redact_transcript(text)
    assert "external_id: ak-ABCDEFGHIJKLMNOPQRSTUVWX" in out          # 身份逐字保留
    assert "[REDACTED_SECRET]" in out                                 # 正文 secret 仍脱敏
    assert "SECRETVALUE1234567890ABCD" not in out


def test_redact_transcript_redacts_title_and_body():
    text = ("---\nsource: cass\nexternal_id: ext-a\nsource_id: local\nagent: codex\n"
            "title: my sk-ant-ABCDEFGHIJKLMNOPQRSTUV key\n---\nbody sk-ant-ABCDEFGHIJKLMNOPQRSTUV\n")
    out = redact_transcript(text)
    assert out.count("[REDACTED_SECRET]") >= 2                        # title + body 都脱敏
    assert "external_id: ext-a" in out                               # 身份不动


def test_redact_transcript_malformed_leaves_frontmatter_untouched():
    # 未闭合 frontmatter → 不改身份区（交自校验兜底拒写）
    text = "---\nexternal_id: ext-a\nsource_id: local\nagent: codex\nno closing fence\n"
    assert redact_transcript(text) == text


def test_redact_transcript_no_frontmatter_redacts_all():
    text = "no frontmatter but token=SECRETVALUE1234567890ABCD here\n"
    assert "[REDACTED_SECRET]" in redact_transcript(text)


def test_redact_transcript_no_secret_is_identity():
    """无 secret 的正常 transcript → redact_transcript 逐字节等于原文（钉住 split/join 拼接精度，
    不掉/多换行；codex R2 P2）。"""
    text = ("---\nsource: cass\nexternal_id: ext-a\nsource_id: local\nagent: codex\n"
            "title: 普通标题\n---\n\n### User\n第一段\n\n第二段无密钥\n")
    assert redact_transcript(text) == text
