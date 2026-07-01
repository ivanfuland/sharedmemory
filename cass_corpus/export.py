# cass_corpus/export.py
# 驱动:选会话 → 读 → Pruner 清洗 → 渲染 → 写 transcript 文件到 gbrain session_corpus 目录。
# 用法:uv run python -m cass_corpus.export [out_dir] [limit]
import os
import sys
from cass_corpus import reader, render
from cass_corpus import state as _state
from cass_corpus.pruner import DeterministicPruner


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
           min_turns=4, max_turns=None, min_chars=2000, pruner=None, since_ts=None):
    pruner = pruner or DeterministicPruner()
    os.makedirs(out_dir, exist_ok=True)
    convs = reader.select_conversations(db_path, limit, agents, min_turns, max_turns, since_ts=since_ts)
    written, skipped, errors = [], [], []
    max_ts, broke = None, False          # D2: max_ts = 无错 ASC 前缀的最大 last_ts(= 新水位线候选)
    for meta in convs:                   # since_ts 给值时 reader 已 ASC 排序
        had_error = False
        try:                                            # per-conversation 隔离:单会话失败不中断整批
            msgs = reader.read_messages(db_path, meta["id"])
            text = render.render(meta, pruner.prune(msgs))
            if len(text) < min_chars:                   # gbrain minChars 默认 2000 会丢,先本地跳过
                skipped.append((meta["id"], len(text)))
            else:
                fn = render.transcript_filename(meta)
                _atomic_write(os.path.join(out_dir, fn), text)
                written.append((fn, len(text), meta.get("title", "")))
        except Exception as e:
            errors.append((meta.get("id"), repr(e)[:200]))
            had_error = True
        if had_error:
            broke = True                 # 一旦出错,停止推进 max_ts(留给下轮重试,零静默丢失)
        elif not broke and meta.get("last_ts") is not None:
            max_ts = meta["last_ts"]     # ASC,无错前缀持续推进
    return {"written": written, "skipped": skipped, "errors": errors,
            "total": len(convs), "max_ts": max_ts}


def run_feed(db_path, out_dir, cap, state_path, backfill=False):
    """水位线编排:读水位线 → export 增量 → 按 max_ts 推进水位线 → 存。
    首跑(无水位线且非 backfill):播种水位线=全库 max ts + courtesy 导最新 cap 条(不 backfill 存量)。"""
    wm = 0 if backfill else _state.load_watermark(state_path)
    if wm is None:
        seed = reader.max_conversation_ts(db_path)                 # D1 首跑播种
        rep = export(db_path, out_dir, limit=cap, since_ts=None)   # 旧 DESC newest-N
        if seed is not None:
            _state.save_watermark(state_path, seed)
        rep["seeded"] = seed
        return rep
    rep = export(db_path, out_dir, limit=cap, since_ts=wm)         # 增量 >= 水位线
    if rep["max_ts"] is not None:
        _state.save_watermark(state_path, rep["max_ts"])
    return rep


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    backfill = "--backfill" in sys.argv[1:]
    db = os.environ.get("CASS_CANON_DB",
                        os.path.expanduser("~/.local/share/coding-agent-search/agent_search.db"))
    out = args[0] if len(args) > 0 else os.path.expanduser("~/.local/share/gbrain/cass-transcripts-poc")
    cap = int(args[1]) if len(args) > 1 else 200
    sp = _state.default_state_path()
    rep = run_feed(db, out, cap=cap, state_path=sp, backfill=backfill)
    print(f"out_dir={out}")
    print(f"written={len(rep['written'])}  skipped={len(rep['skipped'])}  errors={len(rep['errors'])}  of {rep['total']} selected")
    for fn, n, title in rep["written"]:
        print(f"  {n:7d}  {fn}   {title[:40]}")


if __name__ == "__main__":
    main()
