#!/usr/bin/env python3
"""T13 隔离闭集核分类器：解析 `strace -f -y -e trace=%file,ftruncate,fallocate` 的输出，
按 plan v5.1 T13 段接口②规则（+ 控制面 2026-09-05 现场核实后的勘误）逐事件判定放行/违规。

用法：
    strace -f -y -e trace=%file,ftruncate,fallocate -o strace.log -- <被测命令>
    CANON_DATA_DIR=... NEW_DATA_DIR=... CASS_WRITE_LOCK=... REINGEST_LOG_ROOT=... \
    MIRROR_HOME=... XDG_CONFIG_HOME=... TMPDIR=... \
    python3 t13_isolation_closed_set_classifier.py strace.log out.json

允许根从环境变量读取（与 full-reingest.sh 自身的 env 命名一致，同一份 env block 可复用）：
CANON_DATA_DIR / NEW_DATA_DIR / CASS_WRITE_LOCK / REINGEST_LOG_ROOT 必填；
MIRROR_HOME / XDG_CONFIG_HOME / TMPDIR 可缺省（缺省则该根不参与放行判定）。
`T13_STRACE_STARTUP_CWD` 可选，指定被跟踪进程组的启动 cwd（默认取本进程自身 cwd——
约定分类器与当初 `strace -o ... -- bash full-reingest.sh` 在同一 cwd 下调用）。

退出码：0 = 违规清单为空（且无未枚举 syscall）；1 = 有违规或有未枚举 syscall 需人工复核。

规则来源与历史：
  ① link/linkat：仅当**任一**路径在允许根之外才违规（原文"无论路径一律违规"改窄——同根内
     原子写(如 raw-mirror 的 tmp→blob hardlink)是健康行为，只有跨根硬链才是真逃逸）。
  ② 设备节点显式允许清单：仅 /dev/null、/dev/tty、/dev/urandom、/dev/random 这四个，
     不做 /dev/* 通配放行。
  ③ 返回 -1 且 errno ∈ {EEXIST, ENOENT, ENXIO, EACCES} 的写类事件不算违规（没有发生副作用），
     但单列"失败写尝试"计数供人审，不悄悄吞掉。
  ④ TMPDIR 补根：2026-09-05 现场发现 wrapper 的 `env -i` 不转发 TMPDIR/SQLITE_TMPDIR，
     SQLite 落到系统默认 `/var/tmp`，逃出隔离——已在 wrapper 侧修（钉 TMPDIR=SQLITE_TMPDIR=
     run root 下的 tmp 目录），本分类器随之补上 TMPDIR 允许根，否则该修复自己的产物会被
     误判违规。
  ⑤ 相对路径按 pid 分别跟踪 cwd（`mkdir`/`chdir`/`rmdir`/`rename`/`link` 等 bare 版本不带
     dirfd，路径相对于该 pid 当前 cwd 解析，不是相对于本分类器脚本的 cwd）：一律禁用
     `os.path.realpath()` 对相对路径的隐式"相对于本进程 cwd"语义，改为显式用每个 pid 已知的
     cwd 拼接后再判定；cwd 状态源＝该 pid 见过的最近一次 `AT_FDCWD<...>` 装饰或成功的
     `chdir()` 调用。首次见到某 pid 且尚无 cwd 线索时回退到 `T13_STRACE_STARTUP_CWD`。
  ⑥ chdir 归非写类：chdir 本身不写盘、不改数据，只是进程状态导航（`mkdir -p` 之类 coreutils
     实现常见的"chdir 进父目录再相对路径操作"手法会大量触发它）。不查允许根、不计违规；
     离开允许根的导航单列 CWD_NAVIGATION_OUTSIDE 计数供人审，不悄悄吞掉。
"""
from __future__ import annotations

import json
import os
import re
import sys

DEVICE_ALLOWLIST = {"/dev/null", "/dev/tty", "/dev/urandom", "/dev/random"}
FAILED_ERRNOS = {"EEXIST", "ENOENT", "ENXIO", "EACCES"}

READONLY_SYSCALLS = {
    "newfstatat", "statx", "stat", "lstat", "fstat", "statfs",
    "access", "readlink", "readlinkat",
    "getdents", "getdents64",
    "execve",
}
OPEN_SYSCALLS = {"openat", "open"}
LINK_SYSCALLS = {"link", "linkat"}
NA_SYSCALLS = {"getcwd"}
CWD_NAV_SYSCALLS = {"chdir"}
WRITEISH_SYSCALLS = {
    "mkdir", "mkdirat", "rmdir", "unlink", "unlinkat", "rename", "renameat", "renameat2",
    "chmod", "fchmodat", "ftruncate", "fallocate", "creat", "truncate",
}

WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND")

LINE_RE = re.compile(r"^(\d+)\s+([a-zA-Z0-9_]+)\((.*)\)\s*=\s*(\S.*)$")
UNFINISHED_RE = re.compile(r"^(\d+)\s+([a-zA-Z0-9_]+)\((.*?)\s*<unfinished")
RESUMED_RE = re.compile(r"^(\d+)\s+<\.\.\.\s+([a-zA-Z0-9_]+)\s+resumed>.*?\)\s*=\s*(\S.*)$")

PATH_IN_ANGLE_RE = re.compile(r"<([^<>]*)>")
AT_FDCWD_RE = re.compile(r"AT_FDCWD<([^<>]*)>")

REQUIRED_ROOT_ENVS = {
    "CANON": "CANON_DATA_DIR",
    "NEW": "NEW_DATA_DIR",
    "LOCK": "CASS_WRITE_LOCK",
    "REINGEST_LOG_ROOT": "REINGEST_LOG_ROOT",
}
OPTIONAL_ROOT_ENVS = {
    "MIRROR_HOME": "MIRROR_HOME",
    "XDG_CONFIG_HOME": "XDG_CONFIG_HOME",
    "TMPDIR": "TMPDIR",
}


def build_allowed_roots(env: dict) -> dict:
    roots = {}
    missing = []
    for root_name, env_name in REQUIRED_ROOT_ENVS.items():
        val = env.get(env_name)
        if not val:
            missing.append(env_name)
            continue
        roots[root_name] = os.path.realpath(val)
    if missing:
        raise SystemExit(f"missing required env for allowed roots: {', '.join(missing)}")
    for root_name, env_name in OPTIONAL_ROOT_ENVS.items():
        val = env.get(env_name)
        if val:
            roots[root_name] = os.path.realpath(val)
    return roots


class Classifier:
    def __init__(self, allowed_roots: dict, startup_cwd: str):
        self.allowed_roots = allowed_roots
        self.startup_cwd = startup_cwd

    def resolve_path(self, path, cwd):
        """相对路径按给定 cwd 拼接；绝对路径原样 realpath。绝不用 os.getcwd() 隐式语义。"""
        if not path:
            return None
        if not path.startswith("/"):
            path = os.path.join(cwd or self.startup_cwd, path)
        try:
            return os.path.realpath(path)
        except OSError:
            return path

    def under_allowed_root(self, path, cwd):
        real = self.resolve_path(path, cwd)
        if not real:
            return None
        for name, root in self.allowed_roots.items():
            if real == root or real.startswith(root + "/"):
                return name
        return None

    def classify(self, pid: str, syscall: str, argstr: str, rc: str, cwd: str):
        if syscall in NA_SYSCALLS:
            return None

        quoted = extract_quoted_strings(argstr)

        if syscall in CWD_NAV_SYSCALLS:
            path = quoted[0] if quoted else None
            root = self.under_allowed_root(path, cwd)
            if root:
                return {"pid": pid, "syscall": syscall, "verdict": "ALLOW_IN_ROOT", "root": root, "paths": [path]}
            return {"pid": pid, "syscall": syscall, "verdict": "CWD_NAVIGATION_OUTSIDE", "paths": [path]}

        if syscall in LINK_SYSCALLS:
            roots = [self.under_allowed_root(p, cwd) for p in quoted]
            if quoted and all(roots):
                return {"pid": pid, "syscall": syscall, "verdict": "ALLOW_IN_ROOT", "root": roots, "paths": quoted}
            return self._violation_or_failed(syscall, pid, rc, quoted, "link/linkat 至少一个路径在允许根之外")

        if syscall in OPEN_SYSCALLS:
            path = quoted[0] if quoted else None
            if path in DEVICE_ALLOWLIST:
                return {"pid": pid, "syscall": syscall, "verdict": "ALLOW_DEVICE_NODE", "paths": [path]}
            root = self.under_allowed_root(path, cwd)
            if root:
                return {"pid": pid, "syscall": syscall, "verdict": "ALLOW_IN_ROOT", "root": root, "paths": [path]}
            is_write = any(flag in argstr for flag in WRITE_FLAGS)
            if is_write:
                return self._violation_or_failed(syscall, pid, rc, [path], "路径不在允许根之下且带写 flag")
            return {"pid": pid, "syscall": syscall, "verdict": "ALLOW_READONLY_OUTSIDE", "paths": [path]}

        if syscall in READONLY_SYSCALLS:
            path = quoted[0] if quoted else resolve_fd_path(argstr)
            root = self.under_allowed_root(path, cwd)
            if root:
                return {"pid": pid, "syscall": syscall, "verdict": "ALLOW_IN_ROOT", "root": root, "paths": [path]}
            return {"pid": pid, "syscall": syscall, "verdict": "ALLOW_READONLY_OUTSIDE", "paths": [path]}

        if syscall in WRITEISH_SYSCALLS:
            if syscall in ("ftruncate", "fallocate"):
                paths = [resolve_fd_path(argstr)]
            elif syscall in ("rename", "renameat", "renameat2"):
                paths = quoted[:2] if len(quoted) >= 2 else quoted
            else:
                paths = quoted[:1]
            roots = [self.under_allowed_root(p, cwd) for p in paths]
            if paths and all(roots):
                return {"pid": pid, "syscall": syscall, "verdict": "ALLOW_IN_ROOT", "root": roots, "paths": paths}
            return self._violation_or_failed(syscall, pid, rc, paths, "写类事件且至少一个路径不在允许根之下")

        path = quoted[0] if quoted else resolve_fd_path(argstr)
        root = self.under_allowed_root(path, cwd)
        if root:
            return {"pid": pid, "syscall": syscall, "verdict": "ALLOW_IN_ROOT", "root": root, "paths": [path]}
        return {"pid": pid, "syscall": syscall, "verdict": "UNCLASSIFIED",
                 "reason": "未枚举的 syscall 落在允许根之外，人工复核", "paths": [path]}

    @staticmethod
    def _violation_or_failed(syscall, pid, rc, paths, reason):
        if rc_failed_with_allowed_errno(rc):
            return {"pid": pid, "syscall": syscall, "verdict": "FAILED_WRITE_ATTEMPT",
                     "reason": f"{reason}；但 rc={rc} 无副作用，降级不计违规", "paths": paths}
        return {"pid": pid, "syscall": syscall, "verdict": "VIOLATION", "reason": reason, "paths": paths}


def extract_quoted_strings(argstr: str):
    out = []
    i = 0
    n = len(argstr)
    while i < n:
        if argstr[i] == '"':
            j = i + 1
            buf = []
            while j < n and argstr[j] != '"':
                if argstr[j] == "\\" and j + 1 < n:
                    buf.append(argstr[j + 1])
                    j += 2
                    continue
                buf.append(argstr[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
        else:
            i += 1
    return out


def resolve_fd_path(argstr: str):
    m = PATH_IN_ANGLE_RE.search(argstr)
    if m:
        return m.group(1)
    return None


def rc_failed_with_allowed_errno(rc: str):
    if rc is None:
        return False
    if not rc.startswith("-1"):
        return False
    parts = rc.split()
    return len(parts) >= 2 and parts[1] in FAILED_ERRNOS


def iter_events(log_path: str):
    """流式配对 <unfinished>/<resumed>（同一 pid 同一时刻只有一条在飞的调用）；
    产出 (pid, syscall, argstr, rc) 四元组。"""
    pending = {}
    with open(log_path, "r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            rm = RESUMED_RE.match(line)
            if rm:
                pid, _syscall_resumed, rc = rm.groups()
                entry = pending.pop(pid, None)
                if entry is None:
                    continue
                p_syscall, argstr = entry
                yield pid, p_syscall, argstr, rc
                continue
            um = UNFINISHED_RE.match(line)
            if um:
                pid, syscall, argstr = um.groups()
                pending[pid] = (syscall, argstr)
                continue
            m = LINE_RE.match(line)
            if m:
                pid, syscall, argstr, rc = m.groups()
                yield pid, syscall, argstr, rc


def tally(items):
    out = {}
    for it in items:
        out[it["syscall"]] = out.get(it["syscall"], 0) + 1
    return out


def run(log_path: str, out_json: str, allowed_roots: dict, startup_cwd: str) -> int:
    clf = Classifier(allowed_roots, startup_cwd)
    counts = {}
    violations = []
    unclassified = []
    failed_write_attempts = []
    cwd_navigation_outside = []
    total = 0
    skipped_na = 0
    cwd_by_pid = {}

    for pid, syscall, argstr, rc in iter_events(log_path):
        total += 1

        m = AT_FDCWD_RE.search(argstr)
        if m:
            cwd_by_pid[pid] = m.group(1)
        cwd = cwd_by_pid.get(pid, startup_cwd)

        result = clf.classify(pid, syscall, argstr, rc, cwd)

        if syscall == "chdir" and rc == "0":
            quoted = extract_quoted_strings(argstr)
            if quoted:
                cwd_by_pid[pid] = clf.resolve_path(quoted[0], cwd)

        if result is None:
            skipped_na += 1
            continue
        verdict = result["verdict"]
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict == "VIOLATION":
            violations.append(result)
        elif verdict == "UNCLASSIFIED":
            unclassified.append(result)
        elif verdict == "FAILED_WRITE_ATTEMPT":
            failed_write_attempts.append(result)
        elif verdict == "CWD_NAVIGATION_OUTSIDE":
            cwd_navigation_outside.append(result)

    summary = {
        "total_events_parsed": total,
        "skipped_na_getcwd": skipped_na,
        "verdict_counts": counts,
        "violation_count": len(violations),
        "failed_write_attempt_count": len(failed_write_attempts),
        "cwd_navigation_outside_count": len(cwd_navigation_outside),
        "unclassified_count": len(unclassified),
        "allowed_roots": allowed_roots,
        "device_allowlist": sorted(DEVICE_ALLOWLIST),
    }
    with open(out_json, "w") as f:
        json.dump({
            "summary": summary,
            "violations": violations,
            "violations_by_syscall": tally(violations),
            "failed_write_attempts_by_syscall": tally(failed_write_attempts),
            "failed_write_attempts_sample": failed_write_attempts[:30],
            "cwd_navigation_outside_sample": cwd_navigation_outside[:30],
            "unclassified_sample": unclassified[:20],
            "unclassified_by_syscall": tally(unclassified),
        }, f, indent=2)
    print(json.dumps(summary, indent=2))
    return 0 if not violations and not unclassified else 1


def main(argv):
    if len(argv) != 3:
        print(f"usage: {argv[0]} <strace_log> <out_json>", file=sys.stderr)
        return 64
    log_path, out_json = argv[1], argv[2]
    allowed_roots = build_allowed_roots(os.environ)
    startup_cwd = os.environ.get("T13_STRACE_STARTUP_CWD") or os.getcwd()
    return run(log_path, out_json, allowed_roots, startup_cwd)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
