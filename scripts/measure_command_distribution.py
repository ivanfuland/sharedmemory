"""$0 只读统计：tool_result 按命令分类的占比 + 长度分布。

为 spec §5.2「RTK 是否港 / 港哪几个」备料。**测量先于设计。**

⚠️ 只读候选库时必须 `mode=ro`（嵌入进程可能在写）。绝不碰生产库。
"""

from __future__ import annotations

import argparse
import collections
import json
import shlex
import sqlite3
import statistics

from cass_corpus.reader import extra_dict
from everos_adapter.cass_reader import args_to_json_str, parse_tool_name

_BASH_TOOLS = {"Bash", "bash", "exec_command", "shell", "run_command"}


_WRAPPERS = {"sudo", "env", "nohup", "time", "nice"}


def _skip_prefix(toks: list[str]) -> int:
    """跳过 `cd X &&` / wrapper 命令 / 裸 `VAR=VAL` 前缀，返回真实命令的下标。

    codex R0 P2#7：原实现统一靠 `&&` 定位，`sudo pytest` 没有 `&&` -> 静默回落 tool_name。
    codex R1 P2#4：只跳 wrapper 自身不够 —— `sudo -E pytest` 返回 `-E`，
    `env -i pytest` 返回 `-i`，裸的 `FOO=1 pytest` 返回 `FOO=1`。
    故 wrapper 之后要连它的**选项**（`-` 开头）与 **VAR=VAL** 一起跳过。
    """
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok == "cd":                       # `cd X && real_cmd`
            while i < len(toks) and toks[i] != "&&":
                i += 1
            i += 1                            # 跨过 &&
        elif tok in _WRAPPERS:                # `sudo -E real_cmd` / `env -i FOO=1 real_cmd`
            i += 1
            while i < len(toks) and (toks[i].startswith("-") or "=" in toks[i]):
                i += 1
        elif "=" in tok and not tok.startswith("-"):   # 裸的 `FOO=1 real_cmd`
            i += 1
        else:
            break
    return i


def classify(tool_name: str, args: str) -> str:
    if not tool_name:
        return "<unknown>"
    if tool_name not in _BASH_TOOLS:
        return tool_name
    try:
        d = json.loads(args)
    except Exception:
        return tool_name
    cmd = d.get("command") or d.get("cmd") or ""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    if not toks:
        return tool_name
    i = _skip_prefix(toks)
    return toks[i] if i < len(toks) else tool_name


def summarize(rows: list[dict]) -> dict:
    by = collections.defaultdict(list)
    for r in rows:
        by[r["command"]].append(r["chars"])
    out = {}
    for cmd, sizes in by.items():
        sizes.sort()
        out[cmd] = {
            "count": len(sizes),
            "total_chars": sum(sizes),
            "p50": int(statistics.median(sizes)),
            "p95": sizes[min(int(len(sizes) * 0.95), len(sizes) - 1)],
            "max": sizes[-1],
        }
    return out


_COLS = ["extra_bin", "extra_json"]


def collect(db: str, limit: int | None = None) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row          # extra_dict 需要按列名取值

    calls: dict[str, tuple[str, str]] = {}
    for r in con.execute("SELECT content, extra_bin, extra_json FROM messages WHERE role='tool_call'"):
        ex = extra_dict(r, _COLS) or {}
        tcid = ex.get("tool_call_id")
        if isinstance(tcid, str) and tcid:
            # tool_call_args 是 dict；classify 内部 json.loads，故这里 dumps 回字符串
            calls[tcid] = (parse_tool_name(r["content"] or ""), args_to_json_str(ex.get("tool_call_args")))

    rows = []
    q = "SELECT content, extra_bin, extra_json FROM messages WHERE role='tool_result'"
    if limit:
        q += f" LIMIT {int(limit)}"
    for r in con.execute(q):
        ex = extra_dict(r, _COLS) or {}
        name, args = calls.get(ex.get("tool_call_id"), ("", ""))
        rows.append({"command": classify(name, args), "chars": len(r["content"] or "")})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    s = summarize(collect(a.db, a.limit))
    total = sum(v["total_chars"] for v in s.values()) or 1
    print(f"{'command':<22}{'count':>8}{'%chars':>9}{'p50':>8}{'p95':>9}{'max':>10}")
    for cmd, v in sorted(s.items(), key=lambda kv: -kv[1]["total_chars"])[:25]:
        pct = v["total_chars"] / total * 100
        print(f"{cmd:<22}{v['count']:>8}{pct:>8.1f}%{v['p50']:>8}{v['p95']:>9}{v['max']:>10}")


if __name__ == "__main__":
    main()
