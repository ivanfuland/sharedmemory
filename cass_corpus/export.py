# cass_corpus/export.py
# 驱动:选会话 → 读 → Pruner 清洗 → 渲染 → 写 transcript 文件到 gbrain session_corpus 目录。
# 用法:uv run python -m cass_corpus.export [out_dir] [limit]
import os
import re
import stat
import sys
import uuid
from cass_corpus import reader, render
from cass_corpus.redact import redact_secrets, redact_transcript
from cass_corpus import state as _state
from cass_corpus.pruner import DeterministicPruner

# 旧命名:<date>-cass-<agent>-<rowid>.md(末段纯数字)。新命名末段是 `s`+16hex,永不纯数字。
# export 只写不删 → 直接刷进旧目录会新旧并存、gbrain 看到重复/孤儿 transcript。
# 靠"记得先原子换目录"是靠不住的口头约定,这里做成代码级 fail-loud(codex PR#41 R1 审出的 P1)。
# 正则锚到 CASS transcript 形态,否则会误伤目录里任意 `note-123.md`(codex R2 P2)。
_LEGACY_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-cass-.+-\d+\.md$")
_ALLOW_MIXED_ENV = "CASS_CORPUS_ALLOW_MIXED"

# 写目标已存在时的身份校验。
# ⚠ **不能比 session_key**:碰撞的定义就是两个不同会话算出同一个 key(文件名也因此相同),
#   比它两边永远相等。必须比**原像** (external_id, source_id, agent)。
# 覆盖同名但不同身份的文件 = 静默丢一整个会话(codex R2 P1:实测 run_feed 跨轮 + export_one
# 都会无条件 os.replace,errors=[] 零报错)。
_FM_LINE = re.compile(r"^([A-Za-z_]+):\s*(.*)$")
_IDENTITY_KEYS = ("external_id", "source_id", "agent")


class LegacyCorpusDirError(RuntimeError):
    pass


class TranscriptIdentityError(RuntimeError):
    pass


def _parse_frontmatter_identity(s):
    """解析一段文本的 frontmatter 身份。返回 (identity_tuple, dup_keys, status)。
    status: OK(有闭合 ---) / NO_FRONTMATTER(首行非 ---) / UNCLOSED(有起始无闭合)。
    dup_keys: frontmatter 内出现 >1 次的 key（防 §2.3 注入覆盖真身份行）。"""
    lines = s.splitlines()
    if not lines or lines[0].strip() != "---":
        return (("FOREIGN",), set(), "NO_FRONTMATTER")
    fm, counts, closed = {}, {}, False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        m = _FM_LINE.match(line)
        if m:
            k = m.group(1)
            counts[k] = counts.get(k, 0) + 1
            fm[k] = m.group(2).strip()
    dup = {k for k, c in counts.items() if c > 1}
    identity = tuple(fm.get(k) for k in _IDENTITY_KEYS)
    return (identity, dup, "OK" if closed else "UNCLOSED")


def _read_frontmatter_head(path, cap=262144):
    """有界读取：最多 cap 个字符（真实 frontmatter 身份行在顶部、title 是短会话标题，绰绰有余）。
    用单次 bounded read 而非 `for line in f`——后者遇无换行的超长单行会把整行读进内存（128MB body
    没有闭合 fence 时实测 head 达 134MB，cap 形同虚设，codex R2）。text-mode read(cap) 读 ≤cap 字符，
    内存有界；闭合 --- 若在 cap 之外 → 视为 UNCLOSED → FOREIGN（真实文件的 fence 永远在前，安全欠标）。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(cap)


def _identity_of_meta(meta):
    """会话身份原像 (external_id, source_id, agent)。strip 字符串值 → 与 parser 的 group(2).strip()
    对称（消除 raw-meta vs parsed-file 的前后空格不对称，codex R2 P2#3；无空格 fixture 为 no-op）。"""
    return tuple((v.strip() if isinstance(v, str) else v)
                 for v in (meta.get(k) for k in _IDENTITY_KEYS))


def _identity_of_file(path):
    """读已有 transcript 的 frontmatter 身份。无/未闭合 frontmatter、或含重复身份 key → 'FOREIGN'（拒写）。"""
    identity, dup, status = _parse_frontmatter_identity(_read_frontmatter_head(path))
    if status != "OK" or (dup & set(_IDENTITY_KEYS)):   # 重复身份 key 也不可信（codex R1 P1-2）
        return "FOREIGN"
    return identity


def _validate_text_identity(text, meta):
    """写盘前自校验：待写 text 的 frontmatter 身份必须 == meta 身份，且无重复身份 key、
    frontmatter 闭合良好。以 meta 为基准 → legacy(无 ext/source) 与新 schema 都天然涵盖。"""
    identity, dup, status = _parse_frontmatter_identity(text)
    if status != "OK":
        return False
    if dup & set(_IDENTITY_KEYS):
        return False
    return identity == _identity_of_meta(meta)


def _guard_write_target(path, meta):
    """持久 fail-loud：目标已存在且（身份不同 或 非普通文件）→ 绝不覆盖。
    lexists+lstat：symlink/dangling symlink/特殊文件一律按 FOREIGN 拒写（codex R1 P2-2）。"""
    if not os.path.lexists(path):
        return
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise TranscriptIdentityError(
            f"拒绝覆盖 {os.path.basename(path)}：目标不是普通文件（symlink/特殊文件）。")
    prev, cur = _identity_of_file(path), _identity_of_meta(meta)
    if prev != cur:
        raise TranscriptIdentityError(
            f"拒绝覆盖 {os.path.basename(path)}：已有文件身份 {prev}，待写身份 {cur}。"
            f"同名不同会话 = session_key 碰撞或外来文件 —— 绝不静默覆盖。"
            f"若为真碰撞,增大 render._KEY_BYTES 并重刷。")


def _assert_no_legacy_names(out_dir):
    """out_dir 里若有旧 rowid 命名的 transcript → 拒绝写入。
    迁移姿势:导进**全新空目录**,再同盘 mv 原子换上(旧目录留作回滚点)。
    确实要混写(调试)→ 显式 CASS_CORPUS_ALLOW_MIXED=1。"""
    if os.environ.get(_ALLOW_MIXED_ENV) == "1" or not os.path.isdir(out_dir):
        return
    legacy = [n for n in os.listdir(out_dir) if n.endswith(".md") and _LEGACY_NAME.search(n)]
    if legacy:
        raise LegacyCorpusDirError(
            f"{out_dir} 含 {len(legacy)} 个旧 rowid 命名的 transcript(如 {legacy[0]})。"
            f"新命名用稳定 session_key,直接混写会造成新旧并存/孤儿。"
            f"请导进全新空目录后原子 mv 换上;确需混写设 {_ALLOW_MIXED_ENV}=1。")


def _atomic_write(path, text):
    """原子写：先写同目录 tmp 再 os.replace 顶替。tmp 名加 uuid → 同进程多线程也唯一。"""
    tmp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write_transcript(path, text, meta):
    """熔合的原子占位写（替代 _guard_write_target + _atomic_write 两步）。
    ① 写盘前自校验待写内容身份（P0）；② os.link 原子创建，EEXIST → 比原像 → replace(同)/raise(异)。
    危险分支（不同身份新文件竞争创建）由 os.link 原子性保证恰一个成功，窗口消失。"""
    if not _validate_text_identity(text, meta):
        raise TranscriptIdentityError(
            f"拒绝覆盖/写入 {os.path.basename(path)}：待写内容 frontmatter 身份 ≠ 会话身份，"
            f"或 frontmatter 非法（malformed/重复身份 key）—— 绝不落盘被污染的 transcript。")
    tmp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.link(tmp, path)              # 原子创建：仅 path 不存在才成功
            return "created"
        except FileExistsError:
            _guard_write_target(path, meta)  # 既有文件身份校验（lexists）：异 → raise
            os.replace(tmp, path)            # 同身份 = 合法更新，原子覆盖
            return "updated"
    finally:
        try:
            os.unlink(tmp)                   # created 后 tmp 仍在 → 清；replace 后已消失 → 忽略
        except OSError:
            pass                             # 宽捕获：清理失败绝不遮蔽主异常


def export(db_path, out_dir, limit=20, agents=None,
           min_turns=4, max_turns=None, min_chars=2000, pruner=None, since_cursor=None):
    pruner = pruner or DeterministicPruner()
    _assert_no_legacy_names(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    convs = reader.select_conversations(db_path, limit, agents, min_turns, max_turns, since_cursor=since_cursor)
    written, skipped, errors = [], [], []
    max_cursor, broke = None, False      # D2: max_cursor = 无错 (ts,id) ASC 前缀的末端(= 新游标候选)
    for meta in convs:                   # since_cursor 给值时 reader 已按 (ts,id) ASC 排序
        had_error = False
        try:                                            # per-conversation 隔离:单会话失败不中断整批
            msgs = reader.read_messages(db_path, meta["id"])
            text = render.render(meta, pruner.prune(msgs))
            text = redact_transcript(text)                 # ① 脱敏（在 min_chars 门之前）
            if len(text) < min_chars:                   # gbrain minChars 默认 2000 会丢,先本地跳过
                skipped.append((meta["id"], len(text)))
            else:
                fn = render.transcript_filename(meta)
                path = os.path.join(out_dir, fn)
                _write_transcript(path, text, meta)
                written.append((fn, len(text), redact_secrets(meta.get("title") or "")))
        except Exception as e:
            errors.append((meta.get("id"), repr(e)[:200]))
            had_error = True
        if had_error:
            broke = True                 # 一旦出错,停止推进游标(留给下轮重试,零静默丢失)
        elif not broke and meta.get("last_ts") is not None:
            max_cursor = (meta["last_ts"], meta["id"])  # (ts,id) ASC,无错前缀持续推进
    return {"written": written, "skipped": skipped, "errors": errors,
            "total": len(convs), "max_cursor": max_cursor}


def export_one(db_path, out_dir, conv_id=None, min_chars=2000, pruner=None, *, external_id=None):
    """单条精确导出(Inngest F3 逐条驱动用;不碰 cursor)。选择器二选一：
    conv_id（rowid，兼容/调试）或 external_id（稳定键，F3 生产驱动——rowid 跨 CASS 重建会全量重发号）。
    返回 report 含 exported_ts = 实际读到消息的 max created_at(文件真实内容版本,codex R5 P1-2/R6 P2-A)。"""
    if (conv_id is None) == (external_id is None):
        raise ValueError("export_one: exactly one of conv_id / external_id is required")
    pruner = pruner or DeterministicPruner()
    _assert_no_legacy_names(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    meta = (reader.get_conversation(db_path, conv_id) if conv_id is not None
            else reader.get_conversation_by_external_id(db_path, external_id))
    if meta is None:
        return {"written": [], "skipped": [], "errors": [], "total": 0, "exported_ts": None}
    written, skipped, errors = [], [], []
    exported_ts = None
    try:
        # 在 read_messages 之前取内容版本 → exported_ts ≤ 文件实际版本,安全欠标方向
        # (最坏下 tick 冗余 re-feed、gbrain 幂等挡住,绝不丢)。read_messages 返 Msg 无 created_at,
        # 故用专用 max_message_ts(codex R7 P1)。
        exported_ts = reader.max_message_ts(db_path, meta["id"])
        msgs = reader.read_messages(db_path, meta["id"])
        text = render.render(meta, pruner.prune(msgs))
        text = redact_transcript(text)                     # ① 脱敏（在 min_chars 门之前）
        if len(text) < min_chars:
            skipped.append((meta["id"], len(text)))
        else:
            fn = render.transcript_filename(meta)
            path = os.path.join(out_dir, fn)
            _write_transcript(path, text, meta)
            written.append((fn, len(text), redact_secrets(meta.get("title") or "")))
    except Exception as e:
        errors.append((meta.get("id"), repr(e)[:200]))
    return {"written": written, "skipped": skipped, "errors": errors, "total": 1,
            "exported_ts": exported_ts}


def run_feed(db_path, out_dir, cap, state_path, backfill=False):
    """水位线编排:读复合游标 → export 严格 keyset 增量 → 按 max_cursor 推进 → 存。
    首跑(无游标且非 backfill,codex P1-A fix a):**只播种游标=当前最新, import 0**
    (不 courtesy 导——corpus 已由旧 feed 灌过;要灌存量走 --backfill)。
    坏游标文件在 load_cursor 里已 raise(fail loud,codex P1-B),不会落到首跑分支静默重播种。"""
    cursor = (0, 0) if backfill else _state.load_cursor(state_path)
    if cursor is None:
        seed = reader.max_conversation_cursor(db_path)             # 首跑播种=全库最大 (ts,id)
        if seed is not None:
            _state.save_cursor(state_path, seed[0], seed[1])
        return {"written": [], "skipped": [], "errors": [], "total": 0,
                "max_cursor": None, "seeded": seed}
    rep = export(db_path, out_dir, limit=cap, since_cursor=cursor)  # 严格 keyset 增量 > 游标
    if rep["max_cursor"] is not None:
        _state.save_cursor(state_path, rep["max_cursor"][0], rep["max_cursor"][1])
    return rep


def parse_argv(argv):
    """拆 argv → (external_id, positionals, backfill)。
    --external-id 支持空格形与等号形（等号形对以 `--` 开头的值免疫；空格形遇下一 token 是
    另一个 flag 时视为缺值，不吞它）。positional 按位置排除 flag 的值。

    selector flag（--external-id）出现但取不到值 → 一律 raise ValueError（fail-loud，
    Ivan 裁决 2026-07-12，取代此前"缺值→None"的旧语义）。覆盖三种形态：尾随无值
    （`["--external-id"]`）、下一 token 是另一个 flag、等号空值（`["--external-id="]`）。
    理由：缺值→None 会静默改道——例如 `--external-id --backfill /out` 本意是单条导出却因
    缺值悄悄跑成批量 backfill、推进水位线。selector flag 出现本身就表达了"单条导出"意图，
    此时缺值只可能是操作者失误，不该被静默吞成"当作没给"。F3 机器调用路径永远带值，不受此
    收紧影响。

    未知 `--*` flag（含拼写错误，如 `--external_id=` 下划线误用 `--external-id`）一律 raise，
    不再静默忽略——静默忽略会让打错字的 selector 悄悄滑进批量 run_feed（codex 联审 P1-1，
    第三种静默滑批量形态）。唯一例外是 `--backfill`：它在函数末尾单独按 `"--backfill" in argv`
    判定，不受此分支影响，继续被忽略（即不会落进 unknown-flag 分支）。

    `--conv`/`--conv=` CLI 入口已于 2026-07-12 退役（Ivan 拍板）：rowid 跨 CASS 重建会全量
    重发号，不再可作外部驱动键，现在一律落进 unknown-flag fail-loud 分支。rowid 调试需求
    改走：先查出对应的 external_id 后用 `--external-id=` CLI 入口；或在代码里直接调用
    `export_one(conv_id=...)` 函数（该入口保留，供测试与内部调试用）。"""
    eid, positionals, skip_next = None, [], False
    eid_seen = False
    for i, a in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if a == "--external-id":
            eid_seen = True
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            # 下一 token 是 flag（`--`开头）→ 视为缺值；不吞它（否则会把下一个 flag 当成
            # external-id 的值吃掉）。
            eid = None if (nxt is not None and nxt.startswith("--")) else nxt
            skip_next = eid is not None
            continue
        if a.startswith("--external-id="):
            eid_seen = True
            eid = a.split("=", 1)[1] or None
            continue
        if a.startswith("--"):
            if a == "--backfill":     # 唯一合法裸 flag（末尾按 "--backfill" in argv 判定）
                continue
            raise ValueError(f"parse_argv: unknown flag {a!r}")
        positionals.append(a)
    if eid_seen and eid is None:
        raise ValueError("parse_argv: --external-id requires a value")
    return eid, positionals, ("--backfill" in argv)


def main():
    eid, args, backfill = parse_argv(sys.argv[1:])
    db = os.environ.get("CASS_CANON_DB",
                        os.path.expanduser("~/.local/share/coding-agent-search/agent_search.db"))
    out = args[0] if len(args) > 0 else os.path.expanduser("~/.local/share/gbrain/cass-transcripts-poc")
    if eid is not None:
        rep = export_one(db, out, external_id=eid)
    else:
        cap = int(args[1]) if len(args) > 1 else 200
        rep = run_feed(db, out, cap=cap, state_path=_state.default_state_path(), backfill=backfill)
    print(f"out_dir={out}")
    print(f"written={len(rep['written'])}  skipped={len(rep['skipped'])}  errors={len(rep['errors'])}  of {rep['total']} selected")
    print(f"exported_ts={rep.get('exported_ts') or ''}")  # F3 解析它写 fed_msg_ts(codex R6 P1-B:必须在此打印)
    for fn, n, title in rep["written"]:
        print(f"  {n:7d}  {fn}   {title[:40]}")


if __name__ == "__main__":
    main()
