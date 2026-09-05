"""T13 隔离闭集核分类器（infra/cass-semantic/t13_isolation_closed_set_classifier.py）单测。

这九条对照用例是 2026-09-05 现场核实的产物：先构造已知答案的 strace 行，再核对分类器判定，
逐条对应控制面裁定的规则勘误（linkat 改窄、设备白名单、失败写降级、TMPDIR 补根、
cwd 按 pid 跟踪、chdir 归非写类），以及一次真实发现（/var/tmp SQLite 临时文件逃逸）的回归锁。
"""
from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "infra" / "cass-semantic" / "t13_isolation_closed_set_classifier.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("t13_isolation_closed_set_classifier", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _classifier(mod, tmp_path):
    roots = {
        "CANON": str(tmp_path / "canon"),
        "NEW": str(tmp_path / "new"),
        "LOCK": str(tmp_path / "write.lock"),
        "REINGEST_LOG_ROOT": str(tmp_path / "logs"),
        "MIRROR_HOME": str(tmp_path / "mirror-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "TMPDIR": str(tmp_path / "tmp"),
    }
    return mod.Classifier(roots, startup_cwd=str(tmp_path / "startup-cwd")), roots


def _run_line(mod, clf, line, cwd=None):
    m = mod.LINE_RE.match(line)
    pid, syscall, argstr, rc = m.groups()
    return clf.classify(pid, syscall, argstr, rc, cwd or clf.startup_cwd)


def test_var_tmp_sqlite_escape_is_violation_regression_lock(tmp_path):
    """2026-09-05 真实发现的回归锁：SQLite 临时文件写进 /var/tmp（不在任何允许根下）必须 VIOLATION。
    这条曾被怀疑是分类器漏判（控制面 2026-09-05 猜测），逐字核对原始 strace 行后证实分类器本身
    是对的，问题出在别处——但仍然把这行钉成显式回归测试，防止未来重构悄悄放过它。"""
    mod = _load_module()
    clf, _ = _classifier(mod, tmp_path)
    line = ('136958 openat(AT_FDCWD</x>, "/var/tmp/etilqs_733342699aa8a047", '
            'O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, 0600) = 20</var/tmp/etilqs_733342699aa8a047>')
    r = _run_line(mod, clf, line)
    assert r["verdict"] == "VIOLATION"


def test_readonly_open_outside_roots_is_allowed(tmp_path):
    mod = _load_module()
    clf, _ = _classifier(mod, tmp_path)
    line = '136958 openat(AT_FDCWD</x>, "/var/tmp/etilqs_ro", O_RDONLY|O_CLOEXEC) = 20</var/tmp/etilqs_ro>'
    r = _run_line(mod, clf, line)
    assert r["verdict"] == "ALLOW_READONLY_OUTSIDE"


def test_failed_write_with_allowed_errno_downgrades(tmp_path):
    """规则③：EEXIST/ENOENT/ENXIO/EACCES 失败的写类事件没有副作用，降级为 FAILED_WRITE_ATTEMPT，
    不计入 violation，但仍单独留痕供人审。"""
    mod = _load_module()
    clf, _ = _classifier(mod, tmp_path)
    line = '132823 mkdir("/tmp", 0777) = -1 EEXIST (File exists)'
    r = _run_line(mod, clf, line)
    assert r["verdict"] == "FAILED_WRITE_ATTEMPT"


def test_device_node_write_is_allowed_via_explicit_allowlist(tmp_path):
    """规则②：仅 /dev/null、/dev/tty、/dev/urandom、/dev/random 四个精确路径放行，不做 /dev/* 通配。"""
    mod = _load_module()
    clf, _ = _classifier(mod, tmp_path)
    line = ('132833 openat(AT_FDCWD</x>, "/dev/null", O_WRONLY|O_CREAT|O_TRUNC, 0666) '
            '= 3</dev/null>')
    r = _run_line(mod, clf, line)
    assert r["verdict"] == "ALLOW_DEVICE_NODE"


def test_other_device_path_not_in_allowlist_still_checked(tmp_path):
    mod = _load_module()
    clf, _ = _classifier(mod, tmp_path)
    line = ('132833 openat(AT_FDCWD</x>, "/dev/sda", O_WRONLY|O_CREAT|O_TRUNC, 0666) '
            '= 3</dev/sda>')
    r = _run_line(mod, clf, line)
    assert r["verdict"] == "VIOLATION", "/dev/* 不做通配放行，非白名单设备路径仍按写事件判定"


def test_linkat_within_single_allowed_root_is_allowed(tmp_path, ):
    """规则①勘误：linkat 只要两个路径都在同一允许根下（raw-mirror 的 tmp→blob 原子写模式）就放行。"""
    mod = _load_module()
    clf, roots = _classifier(mod, tmp_path)
    line = (f'133035 linkat(AT_FDCWD</x>, "{roots["NEW"]}/raw-mirror/v1/tmp/a.tmp", '
            f'AT_FDCWD</x>, "{roots["NEW"]}/raw-mirror/v1/blobs/b.raw", 0) = 0')
    r = _run_line(mod, clf, line)
    assert r["verdict"] == "ALLOW_IN_ROOT"


def test_linkat_crossing_root_boundary_is_violation(tmp_path):
    """规则①勘误的另一半：只要有一个路径在允许根之外，跨根硬链仍是真逃逸，必须 VIOLATION。"""
    mod = _load_module()
    clf, roots = _classifier(mod, tmp_path)
    line = (f'133035 linkat(AT_FDCWD</x>, "{roots["NEW"]}/raw-mirror/v1/tmp/a.tmp", '
            'AT_FDCWD</x>, "/var/tmp/escaped.raw", 0) = 0')
    r = _run_line(mod, clf, line)
    assert r["verdict"] == "VIOLATION"


def test_mkdir_p_final_leaf_creation_of_new_root_is_allowed(tmp_path):
    """规则⑤回归锁：`mkdir -p "$NEW"` 这类 coreutils 实现会逐级 chdir 进已存在的父目录、
    对每一级都用相对路径 mkdir（真实 pytest tmp_path 往往是 /tmp 下好几层），只有最后一级
    （NEW 本身）真正创建。分类器必须按每个 pid 实际跟踪的 cwd 解析相对路径，不能用自己
    进程的 cwd 去解析，否则会把"创建 NEW 本身"这个正常动作误判成允许根之外的写违规。"""
    mod = _load_module()
    clf, roots = _classifier(mod, tmp_path)
    new_path = pathlib.Path(roots["NEW"])
    components = [c for c in new_path.parts if c not in ("/",)]
    # new_path.parts 对绝对路径首元素是 "/"；重建逐级绝对路径列表：/tmp, /tmp/x, /tmp/x/y, ...
    levels = []
    acc = pathlib.Path("/")
    for c in components:
        acc = acc / c
        levels.append(acc)
    assert levels[-1] == new_path

    pid = "167463"
    seq = []
    # 除最后一级（NEW 本身，尚不存在）外，其余全部已存在（pytest tmp_path 已建好其祖先）。
    seq.append(f'{pid} mkdir("{levels[0]}", 0777) = -1 EEXIST (File exists)')
    seq.append(f'{pid} chdir("{levels[0]}") = 0')
    for lvl in levels[1:-1]:
        seq.append(f'{pid} mkdir("{lvl.name}", 0777) = -1 EEXIST (File exists)')
        seq.append(f'{pid} chdir("{lvl.name}") = 0')
    seq.append(f'{pid} mkdir("{levels[-1].name}", 0777) = 0')

    cwd_by_pid = {}
    results = []
    for line in seq:
        m = mod.LINE_RE.match(line)
        p, syscall, argstr, rc = m.groups()
        cwd = cwd_by_pid.get(p, "/should-never-be-used")
        r = clf.classify(p, syscall, argstr, rc, cwd)
        if syscall == "chdir" and rc == "0":
            quoted = mod.extract_quoted_strings(argstr)
            cwd_by_pid[p] = clf.resolve_path(quoted[0], cwd)
        results.append(r)
    assert results[-1]["verdict"] == "ALLOW_IN_ROOT", results[-1]


def test_chdir_outside_allowed_roots_is_navigation_not_violation(tmp_path):
    """规则⑥：chdir 本身不写盘、无副作用，离开允许根的导航单列 CWD_NAVIGATION_OUTSIDE，不计违规。"""
    mod = _load_module()
    clf, _ = _classifier(mod, tmp_path)
    line = '167373 chdir("/tmp") = 0'
    r = _run_line(mod, clf, line, cwd="/somewhere-not-allowed")
    assert r["verdict"] == "CWD_NAVIGATION_OUTSIDE"


def test_chdir_into_allowed_root_is_allowed(tmp_path):
    mod = _load_module()
    clf, roots = _classifier(mod, tmp_path)
    line = f'167373 chdir("{roots["NEW"]}") = 0'
    r = _run_line(mod, clf, line, cwd="/somewhere-not-allowed")
    assert r["verdict"] == "ALLOW_IN_ROOT"


def test_build_allowed_roots_requires_core_envs(monkeypatch):
    mod = _load_module()
    monkeypatch.delenv("CANON_DATA_DIR", raising=False)
    monkeypatch.delenv("NEW_DATA_DIR", raising=False)
    monkeypatch.delenv("CASS_WRITE_LOCK", raising=False)
    monkeypatch.delenv("REINGEST_LOG_ROOT", raising=False)
    try:
        mod.build_allowed_roots({})
        assert False, "缺必填 env 应 raise SystemExit"
    except SystemExit as e:
        assert "CANON_DATA_DIR" in str(e)
