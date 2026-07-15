"""nightly 对账只读探针:给定候选 session_id 列表,扫实例 markdown,输出 {sid: [entry_ids]}。

只依赖落盘 md 格式(EverOS 公开契约)与 everos_adapter.scan_terminal 公开函数;零写入。
候选走 --sids-file(JSON 数组)而非 argv:候选可达数千,绕开 argv 长度上限。
"""
from __future__ import annotations

import argparse
import json
import pathlib

from everos_adapter.scan_terminal import CASE_GLOB, session_case_entry_ids, session_ids_in


def scan(memory_root: str, sids: list[str]) -> dict[str, list[str]]:
    root = pathlib.Path(memory_root).expanduser()
    out: dict[str, list[str]] = {}
    if not root.exists():
        return out
    want = set(sids)
    for f in sorted(root.rglob(CASE_GLOB)):
        text = f.read_text(encoding="utf-8")
        for sid in set(session_ids_in(text)) & want:   # 先粗筛再按 entry block 精确归属
            ids = session_case_entry_ids(text, sid)
            if ids:
                out.setdefault(sid, []).extend(ids)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--memory-root", required=True)
    ap.add_argument("--sids-file", required=True)
    a = ap.parse_args()
    sids = json.loads(pathlib.Path(a.sids_file).read_text(encoding="utf-8"))
    print(json.dumps(scan(a.memory_root, sids), ensure_ascii=False))


if __name__ == "__main__":
    main()
