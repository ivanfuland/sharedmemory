# cass_corpus/export.py
# 驱动:选会话 → 读 → Pruner 清洗 → 渲染 → 写 transcript 文件到 gbrain session_corpus 目录。
# 用法:uv run python -m cass_corpus.export [out_dir] [limit]
import os
import re
import sys
from cass_corpus import reader, render
from cass_corpus.redact import redact_secrets
from cass_corpus import state as _state
from cass_corpus.pruner import DeterministicPruner

# 旧命名:<date>-cass-<agent>-<rowid>.md(末段纯数字)。新命名末段是 `s`+16hex,永不纯数字。
# export 只写不删 → 直接刷进旧目录会新旧并存、gbrain 看到重复/孤儿 transcript。
# 靠"记得先原子换目录"是靠不住的口头约定,这里做成代码级 fail-loud(codex PR#41 审出的 P1)。
_LEGACY_NAME = re.compile(r"-\d+\.md$")
_ALLOW_MIXED_ENV = "CASS_CORPUS_ALLOW_MIXED"


class LegacyCorpusDirError(RuntimeError):
    pass


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
    """原子写:先写同目录 tmp 再 os.replace 顶替。reader(gbrain autopilot)永远看完整文件,
    不会撞写了一半的残包。失败清理 tmp。"""
    tmp = f"{path}.tmp.{os.getpid()}"
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


def export(db_path, out_dir, limit=20, agents=None,
           min_turns=4, max_turns=None, min_chars=2000, pruner=None, since_cursor=None):
    pruner = pruner or DeterministicPruner()
    _assert_no_legacy_names(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    convs = reader.select_conversations(db_path, limit, agents, min_turns, max_turns, since_cursor=since_cursor)
    written, skipped, errors = [], [], []
    seen_names = {}                      # fn -> conv_id;批内 session_key 碰撞检测
    max_cursor, broke = None, False      # D2: max_cursor = 无错 (ts,id) ASC 前缀的末端(= 新游标候选)
    for meta in convs:                   # since_cursor 给值时 reader 已按 (ts,id) ASC 排序
        had_error = False
        try:                                            # per-conversation 隔离:单会话失败不中断整批
            msgs = reader.read_messages(db_path, meta["id"])
            text = render.render(meta, pruner.prune(msgs))
            text = redact_secrets(text)                 # ① 脱敏（在 min_chars 门之前）
            if len(text) < min_chars:                   # gbrain minChars 默认 2000 会丢,先本地跳过
                skipped.append((meta["id"], len(text)))
            else:
                fn = render.transcript_filename(meta)
                # session_key 碰撞 → 后写覆盖先写 → **静默丢一整个会话**。批内可检测,必须 loud
                # (codex PR#41 C:64bit 在 2424 条上零碰撞,但静默丢数据的失败模式不可接受)。
                if fn in seen_names:
                    raise RuntimeError(f"session_key 碰撞:{fn} 已由 conv {seen_names[fn]} 写出,"
                                       f"conv {meta['id']} 撞名。绝不覆盖 —— 增大 render._KEY_BYTES。")
                seen_names[fn] = meta["id"]
                _atomic_write(os.path.join(out_dir, fn), text)
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


def export_one(db_path, out_dir, conv_id, min_chars=2000, pruner=None):
    """单条精确导出(Inngest F3 逐条驱动用;不碰 cursor)。选一条 → 读 → 清洗 → 渲染 → 原子写。
    返回 report 含 exported_ts = 实际读到消息的 max created_at(文件真实内容版本,codex R5 P1-2/R6 P2-A)。"""
    pruner = pruner or DeterministicPruner()
    _assert_no_legacy_names(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    meta = reader.get_conversation(db_path, conv_id)
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
        text = redact_secrets(text)                     # ① 脱敏（在 min_chars 门之前）
        if len(text) < min_chars:
            skipped.append((meta["id"], len(text)))
        else:
            fn = render.transcript_filename(meta)
            _atomic_write(os.path.join(out_dir, fn), text)
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
    """拆 argv → (conv, positionals, backfill)。
    支持 `--conv 1898` 与 `--conv=1898`（codex 复审 P2：等号形不识别会静默走批量 run_feed 推进水位线）；
    尾随 `--conv` 无值 → conv=None（不 IndexError）；positional 按**位置**排除 --conv 的值
    （不按值排除，避免 out_dir 字符串恰等于 conv-id 时被误吞）。未知 --flag 忽略。"""
    conv, positionals, skip_next = None, [], False
    for i, a in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if a == "--conv":
            conv = argv[i + 1] if i + 1 < len(argv) else None
            skip_next = conv is not None  # 跳过它的值 token（若有）
            continue
        if a.startswith("--conv="):
            conv = a.split("=", 1)[1] or None
            continue
        if a.startswith("--"):
            continue  # 其它 flag（如 --backfill）不进 positional
        positionals.append(a)
    return conv, positionals, ("--backfill" in argv)


def main():
    conv, args, backfill = parse_argv(sys.argv[1:])
    db = os.environ.get("CASS_CANON_DB",
                        os.path.expanduser("~/.local/share/coding-agent-search/agent_search.db"))
    out = args[0] if len(args) > 0 else os.path.expanduser("~/.local/share/gbrain/cass-transcripts-poc")
    if conv is not None:
        rep = export_one(db, out, int(conv))
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
