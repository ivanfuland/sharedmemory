import hashlib

import pytest

from notes_adapter import frontmatter as F
from notes_adapter import identity as I

TAX = {"工作复盘", "Others"}


def test_duplicate_key_fail_loud():
    with pytest.raises(F.DuplicateKeyError):
        F.parse_strict("title: a\ntitle: b\n")


@pytest.mark.parametrize(
    "fm,cat,warn",
    [
        ({"categories": "工作复盘"}, "工作复盘", 0),
        ({"categories": ["垃圾", "工作复盘"]}, "工作复盘", 1),  # 先滤非法再取首个合法
        ({"categories": [123, {}]}, "Others", 1),
        ({"categories": []}, "Others", 1),
        ({"categories": "不在taxonomy"}, "Others", 1),
        ({}, "Others", 1),
    ],
)
def test_categories_matrix(fm, cat, warn):
    n = F.normalize(fm, "stem", TAX)
    assert n.category == cat and len(n.warnings) >= warn


def test_title_date_tags():
    n = F.normalize({"date": "2024-11-20", "tags": "TBR"}, "文件名", TAX)
    assert n.title == "文件名" and n.date == "2024-11-20" and n.tags == ["TBR"]
    n2 = F.normalize({"title": 123, "tags": [{"x": 1}, "a", "a"]}, "s", TAX)
    assert n2.title == "s" and n2.tags == ["a"]


def test_fingerprint_golden():
    fp = F.ingest_fingerprint(
        "h" * 32,
        {
            "provider": "litellm",
            "model": "v4-pro",
            "params": {"temperature": None},
            "pins": {"everos": "c1", "everalgo": "1.0"},
            "taxonomy_hash": "t1",
            "adapter_version": "m0",
            "strategy_version": "s1",
        },
    )
    assert fp == F.ingest_fingerprint(
        "h" * 32,
        {
            "pins": {"everalgo": "1.0", "everos": "c1"},
            "model": "v4-pro",
            "provider": "litellm",
            "params": {"temperature": None},
            "adapter_version": "m0",
            "taxonomy_hash": "t1",
            "strategy_version": "s1",
        },
    )  # 键序无关
    assert len(fp) == 32


# GOLDEN_FP 派生方式（防循环，见 R4 P2-2）：独立一次性脚本按规范 JSON 字节手工构造并计算，
# 人工核对后作为字面量写死于此，不由 F.ingest_fingerprint 反向生成。脚本见 task-3-report.md。
GOLDEN_FP = "bfd58c59b28c549ca1db414ecf9c7a4e"


def test_fingerprint_literal_golden():  # 字面量黄金值：实现后首跑取值写死，此后任何实现变化必红
    fp = F.ingest_fingerprint(
        "0" * 32,
        {
            "provider": "p",
            "model": "m",
            "params": {},
            "pins": {},
            "taxonomy_hash": "t",
            "adapter_version": "a",
            "strategy_version": "s",
        },
    )
    assert fp == GOLDEN_FP


@pytest.mark.parametrize(
    "raw,expect_date,expect_warn",
    [
        ("2024-11-20", "2024-11-20", 0),
        ("2024-13-40", None, 1),
        ("垃圾", None, 1),
    ],
)
def test_date_matrix(raw, expect_date, expect_warn):
    n = F.normalize({"date": raw}, "s", TAX)
    assert n.date == expect_date and len(n.warnings) >= expect_warn


def test_datetime_tz():
    import datetime

    n = F.normalize({"date": datetime.datetime(2024, 1, 2, 3, 4, 5)}, "s", TAX)
    assert n.date.endswith("+08:00")


def test_prepare_document_end_to_end():
    fake_secret = "sk-proj-" + "x" * 25
    md_text = (
        "---\n"
        "title: 测试笔记\n"
        "categories: 工作复盘\n"
        "tags: [a, b]\n"
        "date: 2024-11-20\n"
        "---\n"
        f"body with a seeded fake secret {fake_secret} inline\n"
    )
    staged_bytes = md_text.encode("utf-8")
    staged_sha256 = hashlib.sha256(staged_bytes).hexdigest()
    ref = I.SampleRef(
        canonical_path="工作复盘/测试笔记.md",
        staged_relpath="staged/n-测试笔记.md",
        vault_raw_hash="deadbeef" * 4,
        staged_sha256=staged_sha256,
    )
    cfg = {
        "provider": "litellm",
        "model": "v4-pro",
        "params": {"temperature": None},
        "pins": {"everos": "c1", "everalgo": "1.0"},
        "taxonomy_hash": "t1",
        "adapter_version": "m0",
        "strategy_version": "s1",
    }
    doc = F.prepare_document(ref, staged_bytes, generation=2, taxonomy=TAX, cfg=cfg)

    # source_token 含代际标记
    assert doc.source_token.endswith("-g2.md")
    assert doc.source_token == I.source_token(ref.canonical_path, 2)

    # category_id 直传
    assert doc.category_id == "工作复盘"
    assert doc.title == "测试笔记"

    # redacted 字节不含种入的假密钥
    assert fake_secret.encode("utf-8") not in doc.redacted
    assert b"[REDACTED_SECRET]" in doc.redacted

    # raw_hash 为 ledger desired_hash 直通（vault 原文件字节的 hash），不是从 staged 重算
    assert doc.raw_hash == ref.vault_raw_hash

    # redacted_hash / fingerprint 与手算一致
    assert doc.redacted_hash == I.content_hash(doc.redacted)
    assert doc.fingerprint == F.ingest_fingerprint(doc.redacted_hash, cfg)


def test_prepare_document_rejects_sha256_mismatch():
    staged_bytes = b"---\ntitle: x\n---\nbody\n"
    ref = I.SampleRef(
        canonical_path="a.md",
        staged_relpath="staged/a.md",
        vault_raw_hash="ab" * 16,
        staged_sha256="0" * 64,  # deliberately wrong
    )
    with pytest.raises(ValueError):
        F.prepare_document(ref, staged_bytes, generation=1, taxonomy=TAX, cfg={
            "provider": "p", "model": "m", "params": {}, "pins": {},
            "taxonomy_hash": "t", "adapter_version": "a", "strategy_version": "s",
        })
