"""Reference implementation of the spec's Appendix A digest probe.

Source: `docs/projects/shared-memory/specs/2026-07-09-cass-data-dir-backup-design.md`
(cc-workspace control-plane repo), Appendix A, lines 1203-1222 (`§5.5 腿 4 前缀摘要`
python heredoc). Copied verbatim except for wrapping the script body in a callable
`compute_digest()` function (the original is a `python3 - "$SNAP" messages 213195`
CLI heredoc reading `sys.argv`; tests need a plain function to call directly and
compare against `cass_backup_gate.prefix_digests`). The `enc()`/hashing logic below
is byte-for-byte identical to the spec text — this file exists purely to prove that
an independent, literally-transcribed reading of the spec produces the same digest
as the production implementation (V5d3④).

PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 偏好 / 基建拓扑 / 真实会话内容。
"""
from __future__ import annotations

import hashlib
import sqlite3
import struct
import sys


def enc(v):
    if v is None:            return b'\x00'
    if isinstance(v, int):   d=str(v).encode();  return b'i'+struct.pack('>Q',len(d))+d
    if isinstance(v, float): return b'r'+struct.pack('>d', v)
    if isinstance(v, str):   d=v.encode('utf-8'); return b't'+struct.pack('>Q',len(d))+d
    d=bytes(v);              return b'b'+struct.pack('>Q',len(d))+d


# 前缀摘要按表选列（spec §5.5 / §2.13 ERRATUM，2026-07-13）：`conversations` 只哈希**不可变身份列**
# （排除可变 rollup/尾部列 `ended_at`/`last_message_idx`/… 否则旧会话续写会误 FAIL）；其余表（messages）
# 全列。这里独立硬编码同一子集，与 `cass_backup_gate.LEG4_PREFIX_COLUMNS` 相互印证。
_PREFIX_COLS = {
    "conversations": ("id", "external_id", "started_at", "source_id", "agent_id", "workspace_id"),
}


def compute_digest(db: str, table: str, maxid: int) -> str:
    """逐字对应 spec 附录 A 的探针脚本主体（打开只读快照、算 header、逐行喂 `enc()`）。"""
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    all_cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    sel = _PREFIX_COLS.get(table)
    cols = all_cols if sel is None else ['id'] + [c for c in sel if c != 'id']  # id 置首
    collist = ",".join(f'"{c}"' for c in cols)
    h = hashlib.sha256()
    h.update(struct.pack('>Q', len(cols)))                 # header 也走长度前缀，不用 '|'.join
    for c in cols:
        d = c.encode('utf-8'); h.update(struct.pack('>Q', len(d))); h.update(d)
    for row in con.execute(f'SELECT {collist} FROM "{table}" WHERE id<=? ORDER BY id', (maxid,)):
        h.update(struct.pack('>Q', len(row)))
        for v in row: h.update(enc(v))
    con.close()
    return h.hexdigest()


if __name__ == "__main__":
    db, table, maxid = sys.argv[1], sys.argv[2], int(sys.argv[3])
    digest = compute_digest(db, table, maxid)
    cols = [r[1] for r in sqlite3.connect(f"file:{db}?immutable=1", uri=True).execute(
        f'PRAGMA table_info("{table}")'
    )]
    print(digest, f"cols={len(cols)}")
