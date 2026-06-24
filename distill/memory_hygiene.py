# distill/memory_hygiene.py
# MEMORY.md 卫生检查 — 只读 dry-run，永不写 MEMORY.md / 永不删除（spec R6）
import re
from datetime import datetime, timezone
from distill import writer

_BULLET = re.compile(r"^\s*-\s+(.*\S)\s*$")

def parse_entries(md_text):
    """抽取 MEMORY.md 中所有 '- ' bullet 条目，返回纯文本列表。"""
    return [m.group(1).strip() for line in md_text.splitlines() if (m := _BULLET.match(line))]

def analyze(cfg, token, memory_md_path, out_path, _call=None):
    """对每条 MEMORY.md 条目搜 gbrain，疑似已在记忆层的写 dry-run 提案文件。
    只读：只打开 memory_md_path 读取，绝不写入 / 绝不删除任何内容（spec R6）。
    返回 {"entries": int, "proposals": int}。
    """
    entries = parse_entries(open(memory_md_path, encoding="utf-8").read())
    proposals = []
    for e in entries:
        probe = e.split("：", 1)[0] if "：" in e else e[:12]   # 取条目主题词搜
        hits = writer.search_slugs(cfg, token, probe, _call=_call)
        if hits:
            proposals.append({"entry": e, "suspect_pages": [h[0] for h in hits]})
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# MEMORY.md 卫生 dry-run 提案（{datetime.now(timezone.utc).isoformat()}）\n\n")
        f.write("> 只读分析，**永不自动删除/修改 MEMORY.md**（spec R6）。人工逐条判断。\n\n")
        for p in proposals:
            f.write(f"- 疑似已入记忆层：`{p['entry']}`\n  - 候选页：{', '.join(p['suspect_pages'])}\n")
        if not proposals:
            f.write("（无疑似重复条目）\n")
    return {"entries": len(entries), "proposals": len(proposals)}
