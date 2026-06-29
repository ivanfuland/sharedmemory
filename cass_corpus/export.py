# cass_corpus/export.py
# 驱动:选会话 → 读 → Pruner 清洗 → 渲染 → 写 transcript 文件到 gbrain session_corpus 目录。
# 用法:uv run python -m cass_corpus.export [out_dir] [limit]
import os
import sys
from cass_corpus import reader, render
from cass_corpus.pruner import DeterministicPruner


def export(db_path, out_dir, limit=20, agents=None,
           min_turns=4, max_turns=None, min_chars=2000, pruner=None):
    pruner = pruner or DeterministicPruner()
    os.makedirs(out_dir, exist_ok=True)
    convs = reader.select_conversations(db_path, limit, agents, min_turns, max_turns)
    written, skipped = [], []
    for meta in convs:
        msgs = reader.read_messages(db_path, meta["id"])
        text = render.render(meta, pruner.prune(msgs))
        if len(text) < min_chars:                       # gbrain minChars 默认 2000 会丢,先本地跳过
            skipped.append((meta["id"], len(text)))
            continue
        fn = render.transcript_filename(meta)
        with open(os.path.join(out_dir, fn), "w", encoding="utf-8") as f:
            f.write(text)
        written.append((fn, len(text), meta.get("title", "")))
    return {"written": written, "skipped": skipped, "total": len(convs)}


def main():
    db = os.environ.get("CASS_CANON_DB",
                        os.path.expanduser("~/.local/share/coding-agent-search/agent_search.db"))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.local/share/gbrain/cass-transcripts-poc")
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    rep = export(db, out, limit=limit)
    print(f"out_dir={out}")
    print(f"written={len(rep['written'])}  skipped(<2000 chars)={len(rep['skipped'])}  of {rep['total']} selected")
    for fn, n, title in rep["written"]:
        print(f"  {n:7d}  {fn}   {title[:40]}")


if __name__ == "__main__":
    main()
