"""`infra/backup/cass/cass_sessions.py` + `backup-cass.sh` step 13b-13d 的测试
（Task 11：sessions 通道 A —— 源端前缀校验 / jsonl-only include 过滤 / itemize
解析，spec §6.3.1 / 数据流 step 13b-13d）。

覆盖 Task 11 brief 的 Step 1-3：

  - V12k 系列：`parse_itemize` 对真实 `rsync -ai` 输出的分流——空目标嵌套目录
    （`cd+++++++++` 行被忽略不炸）、新增子目录、只改一个文件（恰 1 行 `>f`）；
    反例：只认 `^>f` 的劣化解析器会在目录行上 exit 1（测试内写劣化版对照，证明
    「忽略 cd/.d/.f」不是可有可无的细节）；未知行 fail-closed。
  - V12a/V12b/V12b2/V12c/V12d/V12e：`check_source` 的截断判定 / 前缀改写判定 /
    `--append` 语义反例演示 / 接口级不读 DEST / quarantine 通道。
  - DEV-1（jsonl-only 过滤）+ codex R1-P1（filter 顺序）+ 跨 root 同名不碰撞。
  - e2e（`run_backup` 全脚本）：V12a/V12b 的「排除 + 不发布」两件事同时成立。

大多数 e2e 测试依赖真 `cass` 二进制构建 `synth_dd`（`requires_cass`，同
test_blobs_manifests.py 的约定）；`check_source`/`parse_itemize` 的纯 Python
单元测试与直接调用真 `rsync` 二进制的对照演示不需要 `cass`。

本文件自包含，不跨文件 import 其它测试文件的私有函数（同代码库既有约定）。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import blake3
import pytest

import cass_common
import cass_sessions
from cass_common import SessionRec

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
SESSIONS_SCRIPT = REPO / "infra" / "backup" / "cass" / "cass_sessions.py"
BACKUP_SCRIPT = REPO / "infra" / "backup" / "backup-cass.sh"

requires_cass = pytest.mark.skipif(
    shutil.which("cass") is None, reason="需要真 cass 二进制构建 synth_dd"
)


# ---------------------------------------------------------------------------
# 帮手
# ---------------------------------------------------------------------------


def _rec(relpath: str, content: bytes, status: str = "present") -> SessionRec:
    return SessionRec(relpath, len(content), blake3.blake3(content).hexdigest(), status)


def _rsync_capture(args: list[str], timeout: int = 30) -> str:
    result = subprocess.run(["rsync", *args], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _write_verified_doctor_stub(home: pathlib.Path, manifests_dir: pathlib.Path) -> None:
    """同 test_blobs_manifests.py 的写法——Tier 0 门必须先 PASS 才能走到本 task
    覆盖的 step 13。本文件自包含一份，不跨文件 import。"""
    import json

    import cass_manifest_census

    census, _ = cass_manifest_census.census_manifests(manifests_dir)
    summary = {
        "missing_blob_count": 0,
        "checksum_mismatch_count": 0,
        "manifest_checksum_mismatch_count": 0,
        "invalid_manifest_count": 0,
        "interrupted_capture_count": 0,
        "manifest_count": census.manifest_count,
        "verified_blob_count": census.unique_blobs,
        "duplicate_blob_reference_count": census.duplicate_refs,
    }
    doc = {"raw_mirror": {"status": "verified", "summary": summary}}
    (home / ".cass-stub-doctor.json").write_text(json.dumps(doc), encoding="utf-8")


def _run(
    tmp_home, run_backup, synth_dd, cass_stub, dest, staging, stamp, session_roots,
    extra_env=None,
):
    """跑一次 backup-cass.sh，固定 stamp + 自定义 CASS_BACKUP_SESSION_ROOTS（测试
    用独立 root 目录，不依赖 tmp_home 的默认三个 alias）。"""
    _write_verified_doctor_stub(tmp_home, synth_dd / "raw-mirror" / "v1" / "manifests")
    env = {
        "CASS_DATA_DIR": str(synth_dd),
        "CASS_BACKUP_DEST": str(dest),
        "CASS_BACKUP_STAGING": str(staging),
        "CASS_BACKUP_STAMP": stamp,
        "CASS_BACKUP_SESSION_ROOTS": session_roots,
        "PATH": f"{cass_stub}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    if extra_env:
        env.update(extra_env)
    rc, out, _dest = run_backup(env=env)
    return rc, out


# ---------------------------------------------------------------------------
# Step 1 — V12k: parse_itemize 对真实 rsync -ai 输出的分流
# ---------------------------------------------------------------------------


def test_v12k_nested_dirs_into_empty_target_ignores_cd_lines(tmp_path):
    src = tmp_path / "src"
    (src / "a" / "b").mkdir(parents=True)
    (src / "a" / "b" / "f.jsonl").write_text('{"x":1}\n')
    dst = tmp_path / "dst"
    dst.mkdir()

    itemize_text = _rsync_capture(["-ai", "--append", "--prune-empty-dirs", f"{src}/", f"{dst}/"])
    assert "cd+++++++++" in itemize_text, itemize_text  # 夹具自检：确实产生了目录行

    itemize_file = tmp_path / "itemize.txt"
    itemize_file.write_text(itemize_text)
    rc, transferred = cass_sessions.parse_itemize(str(itemize_file))

    assert rc == 0, itemize_text
    assert transferred == ["a/b/f.jsonl"], itemize_text


def test_v12k_reflex_degraded_parser_only_accepting_gtf_fails_on_dir_lines(tmp_path):
    """反例：只认 `^>f`、把其余一律当未知行的劣化解析器，对同一份真实 itemize
    输出会在第一条目录行上判失败——证明「^cd/^.d/^.f 必须忽略」不是可有可无的
    细节，而是 spec step 13d 明确要求的分流规则（spec 原文：「只认 ^>f 而把其余
    行当解析失败 ⇒ 首次同步直接 exit 1，备份通道当场不可用」）。"""
    src = tmp_path / "src"
    (src / "a" / "b").mkdir(parents=True)
    (src / "a" / "b" / "f.jsonl").write_text('{"x":1}\n')
    dst = tmp_path / "dst"
    dst.mkdir()
    itemize_text = _rsync_capture(["-ai", "--append", "--prune-empty-dirs", f"{src}/", f"{dst}/"])

    def _degraded_only_gtf(text: str) -> int:
        for line in text.splitlines():
            if not line:
                continue
            if line.startswith(">f"):
                continue
            return 1  # 劣化版：其余（含目录行）一律判为未知行
        return 0

    assert _degraded_only_gtf(itemize_text) == 1, (
        f"劣化解析器理应在目录行上判失败，若它意外 PASS 说明这份夹具没有产生目录行: {itemize_text}"
    )


def test_v12k_new_subdirectory_added_on_second_sync(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()
    (src / "first.jsonl").write_text('{"a":1}\n')
    _rsync_capture(["-ai", "--append", "--prune-empty-dirs", f"{src}/", f"{dst}/"])  # 第一轮基线

    (src / "newdir").mkdir()
    (src / "newdir" / "second.jsonl").write_text('{"b":1}\n')
    itemize_text = _rsync_capture(["-ai", "--append", "--prune-empty-dirs", f"{src}/", f"{dst}/"])

    itemize_file = tmp_path / "itemize.txt"
    itemize_file.write_text(itemize_text)
    rc, transferred = cass_sessions.parse_itemize(str(itemize_file))

    assert rc == 0, itemize_text
    assert transferred == ["newdir/second.jsonl"], itemize_text


def test_v12k_single_file_change_produces_exactly_one_itemize_line(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()
    (src / "unchanged.jsonl").write_text('{"a":1}\n')
    (src / "grows.jsonl").write_text('{"b":1}\n')
    _rsync_capture(["-ai", "--append", "--prune-empty-dirs", f"{src}/", f"{dst}/"])  # 基线

    with open(src / "grows.jsonl", "a") as f:
        f.write('{"b":2}\n')
    itemize_text = _rsync_capture(["-ai", "--append", "--prune-empty-dirs", f"{src}/", f"{dst}/"])

    lines = [line for line in itemize_text.splitlines() if line]
    assert len(lines) == 1, itemize_text  # 恰 1 行——未改动的文件不出现

    itemize_file = tmp_path / "itemize.txt"
    itemize_file.write_text(itemize_text)
    rc, transferred = cass_sessions.parse_itemize(str(itemize_file))
    assert rc == 0
    assert transferred == ["grows.jsonl"]


def test_parse_itemize_unknown_line_fails_closed(tmp_path):
    itemize_file = tmp_path / "itemize.txt"
    itemize_file.write_text(">f+++++++++ ok.jsonl\n*deleting   ghost.jsonl\n")
    rc, transferred = cass_sessions.parse_itemize(str(itemize_file))
    assert rc == 1
    assert transferred == []


def test_parse_itemize_receiver_only_lt_prefix_also_fails_closed(tmp_path):
    """`<f...`（receiver→sender 方向的传输标记，本通道单向不该出现）同样是未知行。"""
    itemize_file = tmp_path / "itemize.txt"
    itemize_file.write_text("<f.st....... weird.jsonl\n")
    rc, transferred = cass_sessions.parse_itemize(str(itemize_file))
    assert rc == 1
    assert transferred == []


def test_parse_itemize_blank_lines_ignored(tmp_path):
    itemize_file = tmp_path / "itemize.txt"
    itemize_file.write_text(">f+++++++++ a.jsonl\n\ncd+++++++++ dir/\n\n>f+++++++++ b.jsonl\n")
    rc, transferred = cass_sessions.parse_itemize(str(itemize_file))
    assert rc == 0
    assert transferred == ["a.jsonl", "b.jsonl"]


def test_parse_itemize_cli_subprocess_pass(tmp_path):
    itemize_file = tmp_path / "itemize.txt"
    itemize_file.write_text(">f+++++++++ a/b/f.jsonl\ncd+++++++++ a/\ncd+++++++++ a/b/\n")
    result = subprocess.run(
        [str(VENV_PY), str(SESSIONS_SCRIPT), "parse-itemize", "--in", str(itemize_file)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "a/b/f.jsonl\n"


def test_parse_itemize_cli_subprocess_unknown_line_exit1_no_stdout(tmp_path):
    itemize_file = tmp_path / "itemize.txt"
    itemize_file.write_text(">f+++++++++ a.jsonl\n<f.st....... weird.jsonl\n")
    result = subprocess.run(
        [str(VENV_PY), str(SESSIONS_SCRIPT), "parse-itemize", "--in", str(itemize_file)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == "", "fail-closed：未知行必须停止输出，不能吐出部分结果"


# ---------------------------------------------------------------------------
# Step 2 — V12a/V12b: check_source 截断 / 前缀改写判定（unit level）
# ---------------------------------------------------------------------------


def test_v12a_truncated_source_excluded_and_exit3(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    good = b"good1\ngood2\ngood3\n"
    (root / "s.jsonl").write_bytes(good)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("a/s.jsonl", good)])

    (root / "s.jsonl").write_bytes(b"good1\n")  # 截断
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(str(state_path), f"a={root}", str(out_dir))

    assert rc == 3
    assert (out_dir / "exclude.a").read_text() == "/s.jsonl\n"


def test_v12b_prefix_rewritten_after_regrow_with_different_content_excluded(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    good = b"good1\ngood2\ngood3\n"
    (root / "s.jsonl").write_bytes(good)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("a/s.jsonl", good)])

    (root / "s.jsonl").write_bytes(b"bad11\nbad22\nbad33\nbad44\n")  # 截断后长回不同内容
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(str(state_path), f"a={root}", str(out_dir))

    assert rc == 3
    assert (out_dir / "exclude.a").read_text() == "/s.jsonl\n"


def test_check_source_unchanged_prefix_passes_clean(tmp_path):
    """正例对照：源端只在旧长度之后增长、前缀不变 —— 不应判异常。"""
    root = tmp_path / "root"
    root.mkdir()
    good = b"good1\ngood2\n"
    (root / "s.jsonl").write_bytes(good)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("a/s.jsonl", good)])

    (root / "s.jsonl").write_bytes(good + b"good3\n")  # 只追加，前缀不变
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(str(state_path), f"a={root}", str(out_dir))

    assert rc == 0
    assert (out_dir / "exclude.a").read_text() == ""


def test_absent_at_source_record_skipped_without_check(tmp_path):
    """`absent_at_source` 记录即便 size/hash 是彻底捏造的假值、文件在源端也确实
    不存在，也必须直接跳过——不比对、不计入异常（源端本来就没有，无从比对）。"""
    root = tmp_path / "root"
    root.mkdir()
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(
        state_path, [SessionRec("a/gone.jsonl", 999999, "f" * 64, "absent_at_source")]
    )
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(str(state_path), f"a={root}", str(out_dir))

    assert rc == 0
    assert (out_dir / "exclude.a").read_text() == ""


def test_present_record_missing_from_source_now_is_skipped_not_excluded(tmp_path):
    """`present` 状态但此刻源端已不存在该文件——13b 无从比对，交由 Task 12 的
    全量回读门处理，本层既不报错也不排除。"""
    root = tmp_path / "root"
    root.mkdir()
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("a/never-here.jsonl", b"x")])
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(str(state_path), f"a={root}", str(out_dir))

    assert rc == 0
    assert (out_dir / "exclude.a").read_text() == ""


def test_state_none_produces_clean_exit0_and_empty_exclude_files_still_written(tmp_path):
    """首晚（state 为 NONE）⇒ 空清单全净；exclude 文件即使空也要为每个 root 生成
    （`rsync --exclude-from` 吃空文件是安全的）。"""
    root_a = tmp_path / "root-a"
    root_a.mkdir()
    root_b = tmp_path / "root-b"
    root_b.mkdir()
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source("NONE", f"a={root_a}:b={root_b}", str(out_dir))

    assert rc == 0
    assert (out_dir / "exclude.a").read_text() == ""
    assert (out_dir / "exclude.b").read_text() == ""


def test_state_record_unknown_root_alias_is_internal_error(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("ghost-alias/x.jsonl", b"x")])
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(str(state_path), f"a={root}", str(out_dir))

    assert rc == 1


# ---------------------------------------------------------------------------
# Step 2 — 反例演示：裸 -a / --append-verify（不经 cass_sessions，直接跑 rsync）
# ---------------------------------------------------------------------------


def test_counter_example_bare_a_overwrites_nas_good_version_with_truncated_source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "s.jsonl").write_bytes(b"good1\ngood2\ngood3\n")
    (src / "s.jsonl").write_bytes(b"good1\n")  # 截断

    _rsync_capture(["-a", f"{src}/", f"{dst}/"])

    assert (dst / "s.jsonl").read_bytes() == b"good1\n", (
        "反例：裸 -a 会把截断后的源端忠实同步过去，覆盖 NAS 上的好版本"
    )


def test_counter_example_append_verify_full_retransmit_on_prefix_mismatch(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "s.jsonl").write_bytes(b"good1\ngood2\ngood3\n")
    (src / "s.jsonl").write_bytes(b"bad1\nbad2\nbad3\nbad4\n")  # 前缀不再匹配

    _rsync_capture(["-a", "--append-verify", f"{src}/", f"{dst}/"])

    assert (dst / "s.jsonl").read_bytes() == b"bad1\nbad2\nbad3\nbad4\n", (
        "反例：--append-verify 的前缀校验失败会触发整份重传，覆盖 NAS 上的好版本"
    )


def test_v12b2_append_never_rewrites_existing_bytes_only_extends_past_old_length(tmp_path):
    """`--append` 是唯一「永不改写已有字节」的原语：旧前缀原封不动，只把源端超出
    旧长度的那几个字节追加上去（即便前缀已经不匹配也不校验、不回滚）。"""
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "s.jsonl").write_bytes(b"good1\ngood2\n")
    (src / "s.jsonl").write_bytes(b"BAD1\nBAD2\nnew3\n")

    _rsync_capture(["-a", "--append", f"{src}/", f"{dst}/"])

    assert (dst / "s.jsonl").read_bytes() == b"good1\ngood2\nw3\n"


def test_v12b2_backup_script_never_uses_append_verify():
    text = BACKUP_SCRIPT.read_text(encoding="utf-8")
    assert "--append-verify" not in text, (
        "sessions 通道必须用 --append，绝不能用 --append-verify（前缀不符会整份重传，"
        "把 NAS 好版本覆盖掉）"
    )


def test_v12c_backup_script_never_uses_backup_flag():
    text = BACKUP_SCRIPT.read_text(encoding="utf-8")
    assert "--backup" not in text, (
        "sessions 通道绝不能用 --backup/--backup-dir——对每一次正常追加都整份保留"
        "旧版本，是成本炸弹"
    )


# ---------------------------------------------------------------------------
# Step 2 — V12d: 接口级不读 DEST/NAS
# ---------------------------------------------------------------------------


def test_v12d_check_source_does_not_require_dest_to_exist(tmp_path):
    """`--state` 指向的内容文件真实存在，但全程不 mkdir 任何叫 "dest"/DEST 的
    目录——check-source 必须照常成功（函数签名/CLI 参数不含任何 NAS 路径）。"""
    root = tmp_path / "root"
    root.mkdir()
    good = b"good1\ngood2\n"
    (root / "s.jsonl").write_bytes(good)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("a/s.jsonl", good)])
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(str(state_path), f"a={root}", str(out_dir))

    assert rc == 0


def test_v12d_check_source_cli_has_no_dest_argument():
    result = subprocess.run(
        [str(VENV_PY), str(SESSIONS_SCRIPT), "check-source", "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--dest" not in result.stdout, (
        f"check-source 的 CLI 参数面不得出现任何 DEST/NAS 路径参数: {result.stdout}"
    )


# ---------------------------------------------------------------------------
# Step 2 — V12e: 人工放行通道（quarantine）
# ---------------------------------------------------------------------------


def test_v12e_quarantine_missing_reason_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(
        "NONE", f"a={root}", str(out_dir), quarantine="a/s.jsonl", quarantine_reason=None,
    )

    assert rc == 1
    assert not out_dir.exists() or not list(out_dir.iterdir()), (
        "内部错误路径不应落任何 exclude 文件"
    )


def test_v12e_quarantine_named_file_excluded_others_proceed_normally(tmp_path):
    """点名的文件排除后其余照常——且 quarantine 本身不计入「异常」，与 rebaseline
    （spec §5.7）同构：只关掉这一个文件的「异常即挡发布」，其余判据照跑。"""
    root = tmp_path / "root"
    root.mkdir()
    good_bad = b"good-a\n"
    good_fine = b"good-b\n"
    (root / "bad.jsonl").write_bytes(good_bad)
    (root / "fine.jsonl").write_bytes(good_fine)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(
        state_path, [_rec("a/bad.jsonl", good_bad), _rec("a/fine.jsonl", good_fine)]
    )
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(
        str(state_path), f"a={root}", str(out_dir),
        quarantine="a/bad.jsonl", quarantine_reason="known corruption, tracked in TICKET-1",
    )

    assert rc == 0, "quarantine 点名的文件不计入异常——其余文件校验全过，整体应视为全净"
    assert (out_dir / "exclude.a").read_text() == "/bad.jsonl\n"


def test_v12e_quarantine_does_not_suppress_unrelated_real_anomaly(tmp_path):
    """quarantine 只豁免它点名的那一个文件；另一个真实异常（未被点名）仍必须
    照常触发 exit 3——quarantine 不是「关掉本轮所有异常检测」的总开关。"""
    root = tmp_path / "root"
    root.mkdir()
    good_bad = b"good-a\n"
    good_other = b"good-c\n"
    (root / "bad.jsonl").write_bytes(good_bad)
    (root / "other.jsonl").write_bytes(good_other)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(
        state_path, [_rec("a/bad.jsonl", good_bad), _rec("a/other.jsonl", good_other)]
    )
    (root / "other.jsonl").write_bytes(b"g")  # 未被 quarantine 的真实截断（比记录短）
    out_dir = tmp_path / "excl"

    rc = cass_sessions.check_source(
        str(state_path), f"a={root}", str(out_dir),
        quarantine="a/bad.jsonl", quarantine_reason="known corruption",
    )

    assert rc == 3, "未被点名的真实异常仍必须触发 exit 3"
    lines = set((out_dir / "exclude.a").read_text().splitlines())
    assert lines == {"/bad.jsonl", "/other.jsonl"}


def test_check_source_cli_subprocess_exit3_on_anomaly(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    good = b"good1\ngood2\n"
    (root / "s.jsonl").write_bytes(good)
    state_path = tmp_path / "state.tsv"
    cass_common.state_write_atomic(state_path, [_rec("a/s.jsonl", good)])
    (root / "s.jsonl").write_bytes(b"good1\n")
    out_dir = tmp_path / "excl"

    result = subprocess.run(
        [
            str(VENV_PY), str(SESSIONS_SCRIPT), "check-source",
            "--state", str(state_path), "--roots", f"a={root}",
            "--out-exclude-dir", str(out_dir),
        ],
        capture_output=True, text=True, timeout=15,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert (out_dir / "exclude.a").read_text() == "/s.jsonl\n"


# ---------------------------------------------------------------------------
# Step 3 — codex R1-P1: rsync filter 顺序（不经 cass_sessions，直接跑 rsync 对照）
# ---------------------------------------------------------------------------


def test_r1p1_filter_order_exclude_from_must_precede_include_jsonl(tmp_path):
    src = tmp_path / "src"
    (src / "a" / "b").mkdir(parents=True)
    (src / "a" / "b" / "s.jsonl").write_text('{"bad":true}\n')
    exclude_file = tmp_path / "exclude.alpha"
    exclude_file.write_text("/a/b/s.jsonl\n")

    dst_correct = tmp_path / "dst-correct"
    dst_correct.mkdir()
    correct = subprocess.run(
        [
            "rsync", "-ai", "--append", "--prune-empty-dirs",
            f"--exclude-from={exclude_file}",
            "--include=*/", "--include=*.jsonl", "--exclude=*",
            f"{src}/", f"{dst_correct}/",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert correct.returncode == 0, correct.stdout + correct.stderr
    assert not (dst_correct / "a" / "b" / "s.jsonl").exists(), (
        f"正确顺序（--exclude-from 排在 include 之前）下 exclude 必须生效: {correct.stdout}"
    )

    dst_wrong = tmp_path / "dst-wrong"
    dst_wrong.mkdir()
    wrong = subprocess.run(
        [
            "rsync", "-ai", "--append", "--prune-empty-dirs",
            "--include=*/", "--include=*.jsonl", "--exclude=*",
            f"--exclude-from={exclude_file}",
            f"{src}/", f"{dst_wrong}/",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert wrong.returncode == 0, wrong.stdout + wrong.stderr
    assert (dst_wrong / "a" / "b" / "s.jsonl").exists(), (
        f"反例：劣化顺序（--exclude-from 排在 include 之后）下，filter first-match 语义"
        f"让 *.jsonl 先命中 include，exclude 对它永不生效（codex R1-P1 实测）；若这里"
        f"反而不存在说明 rsync 行为已变，需要重新核实 spec 的顺序结论: {wrong.stdout}"
    )
    assert ">f+++++++++ a/b/s.jsonl" in wrong.stdout, wrong.stdout


# ---------------------------------------------------------------------------
# Step 3 — e2e: DEV-1 jsonl-only 过滤 + 跨 root 同名不碰撞
# ---------------------------------------------------------------------------


@requires_cass
def test_dev1_jsonl_only_filter_and_empty_dirs_pruned_e2e(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root = tmp_path / "root"
    root.mkdir()
    (root / "auth-profiles.json").write_text("{}")
    (root / "x.md").write_text("notes")
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "s.jsonl").write_text('{"ok":true}\n')
    (root / "empty-dir").mkdir()

    rc, out = _run(
        tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "dev1", f"onlyroot={root}",
    )

    assert rc == 0, out
    synced = dest / "sessions" / "onlyroot"
    assert (synced / "a" / "b" / "s.jsonl").is_file(), out
    assert not (synced / "auth-profiles.json").exists(), out
    assert not (synced / "x.md").exists(), out
    assert not (synced / "empty-dir").exists(), (
        f"--prune-empty-dirs 必须挡住无内容的目录出现在 NAS 上: {list(synced.rglob('*'))}"
    )


@requires_cass
def test_dev1_cross_root_same_name_exclude_isolated_e2e(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """同名文件出现在两个不同 root：exclude 点名 rootA 的那份后，NAS 上 rootA 那
    份必须不出现，而 rootB 的同名文件必须照常同步——证明 exclude 文件是 per-root
    的，不会串味或碰撞（codex R1-P1 后半段关切）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    session_roots = f"aroot={root_a}:broot={root_b}"

    good_a = b"good-a-1\ngood-a-2\n"
    (root_a / "same").mkdir()
    (root_a / "same" / "name.jsonl").write_bytes(good_a)
    (root_b / "same").mkdir()
    (root_b / "same" / "name.jsonl").write_bytes(b"good-b-1\ngood-b-2\n")

    # 手工造共享状态：只给 rootA 的文件记一条「更长」的 nas_size（模拟它此刻已经
    # 被截断）——rootB 的同名文件不在 state 里（对 check-source 而言是全新文件，
    # 不比对，正常同步）。
    cass_common.state_write_atomic(
        dest / "sessions.state.tsv",
        [SessionRec("aroot/same/name.jsonl", len(good_a) + 100, blake3.blake3(good_a).hexdigest(), "present")],
    )
    # aroot 的文件此刻比记录的 nas_size 短 ⇒ 判为截断，排除出本次同步。
    assert len((root_a / "same" / "name.jsonl").read_bytes()) < len(good_a) + 100

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "dev1-cross", session_roots)

    assert rc != 0, out
    assert not (dest / "sessions" / "aroot" / "same" / "name.jsonl").exists(), (
        f"被排除的文件不该在 NAS 上第一次出现: {out}"
    )
    assert (dest / "sessions" / "broot" / "same" / "name.jsonl").read_bytes() == b"good-b-1\ngood-b-2\n", (
        f"另一 root 的同名文件必须照常同步，不受 aroot 的 exclude 影响: {out}"
    )


# ---------------------------------------------------------------------------
# e2e — V12a/V12b：排除 + 不发布两件事同时成立（codex R4-P1 回归）
# ---------------------------------------------------------------------------


@requires_cass
def test_v12a_e2e_truncated_session_blocks_publish_but_syncs_healthy_files(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """首晚（无 state）成功同步一份「好」会话文件；手工种一份共享状态基线
    （模拟 Task 12 的 13e 已经跑过），截断源端后再跑一次 —— 必须同时满足两件事：
    该文件被排除出同步（NAS 好版本原封不动）**且**整次备份不发布（无新
    `cass-*/`、落 `INCOMPLETE-*`），同时一个全新（不在 state 里）的健康文件仍
    照常同步——「healthy 部分照同步，但当晚不发布」不是空话。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root = tmp_path / "root"
    root.mkdir()
    session_roots = f"alpha={root}"

    good = b'{"line":1}\n{"line":2}\n{"line":3}\n'
    session_file = root / "proj" / "s.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_bytes(good)

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12a-first", session_roots)
    assert rc1 == 0, out1
    nas_copy = dest / "sessions" / "alpha" / "proj" / "s.jsonl"
    assert nas_copy.read_bytes() == good, out1

    cass_common.state_write_atomic(dest / "sessions.state.tsv", [_rec("alpha/proj/s.jsonl", good)])

    session_file.write_bytes(b'{"line":1}\n')  # 截断
    healthy_extra = root / "proj" / "extra.jsonl"
    healthy_extra.write_bytes(b'{"new":1}\n')  # 全新、不在 state 里的健康文件

    cass_before = sorted(p.name for p in dest.glob("cass-*"))
    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12a-second", session_roots)

    assert rc2 != 0, out2
    assert (dest / "INCOMPLETE-v12a-second").is_dir(), out2
    assert not (dest / ".incomplete-v12a-second").exists(), out2
    assert sorted(p.name for p in dest.glob("cass-*")) == cass_before, (
        f"不能发布出任何新的 cass-*/: {out2}"
    )
    assert nas_copy.read_bytes() == good, "NAS 好版本必须原封不动（排除 + --append 双保险）"
    assert (dest / "sessions" / "alpha" / "proj" / "extra.jsonl").read_bytes() == b'{"new":1}\n', (
        f"健康（未受影响）的新文件必须照常同步，即便本次整体不发布: {out2}"
    )


@requires_cass
def test_v12b_e2e_prefix_rewrite_blocks_publish_no_complete(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root = tmp_path / "root"
    root.mkdir()
    session_roots = f"alpha={root}"

    session_file = root / "s.jsonl"
    good = b"good1\ngood2\ngood3\n"
    session_file.write_bytes(good)

    rc1, out1 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12b-first", session_roots)
    assert rc1 == 0, out1

    cass_common.state_write_atomic(dest / "sessions.state.tsv", [_rec("alpha/s.jsonl", good)])
    session_file.write_bytes(b"bad11\nbad22\nbad33\nbad44\n")  # 截断后长回不同内容

    rc2, out2 = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "v12b-second", session_roots)

    assert rc2 != 0, out2
    assert (dest / "INCOMPLETE-v12b-second").is_dir(), out2
    assert not list(dest.rglob("COMPLETE")), out2
    assert (dest / "sessions" / "alpha" / "s.jsonl").read_bytes() == good, (
        "--append + exclude 双保险：NAS 前缀必须原封不动"
    )


@requires_cass
def test_first_night_state_none_full_sync_still_succeeds_e2e(
    tmp_home, run_backup, synth_dd, cass_stub, tmp_path
):
    """首晚（`$DEST/sessions.state.tsv` 不存在）⇒ check-source 传 NONE ⇒ 全净、
    整次备份走到临时出口正常 exit 0（本 task 阶段还没有 13a 的完整 ADOPT 语义，
    也不需要——state 不存在时同步照走）。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    staging = tmp_path / "staging"
    root = tmp_path / "root"
    root.mkdir()
    (root / "s.jsonl").write_bytes(b'{"a":1}\n')

    assert not (dest / "sessions.state.tsv").exists()

    rc, out = _run(tmp_home, run_backup, synth_dd, cass_stub, dest, staging, "first-night", f"a={root}")

    assert rc == 0, out
    assert (dest / "sessions" / "a" / "s.jsonl").read_bytes() == b'{"a":1}\n', out
