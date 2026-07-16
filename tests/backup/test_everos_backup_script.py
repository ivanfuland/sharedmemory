"""`infra/backup/backup-everos.sh` 端到端脚本测试(跑真脚本,stub 外部命令)。

背景(2026-07-16):脚本 `set -e` + tar 打包**活目录**。feeder 24/7 写实例文件,
tar 读中撞上写入时 GNU tar 报 "file changed as we read it" 并 exit 1——这对
crash-consistent 备份是设计内可接受的警告,但 `set -e` 把它当致命错 → 备份判死。
修法:区分 tar exit 1(warning,继续)与 exit ≥2(真错,中止)。

测试策略照 test_script_guards.py:pytest 跑真脚本,PATH 前置 stub tar——
stub 只在 create(-czf)调用上注入退出码,其余调用(-tzf 验证)透传真 tar。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "infra" / "backup" / "backup-everos.sh"
REAL_TAR = shutil.which("tar")


def _mk_instance(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """最小 everos 实例:BASE/env + BASE/root/ 若干文件;DEST 在 tmp(非 NAS 前缀,跳过挂载 guard)。"""
    base = tmp_path / "everos-prod"
    root = base / "root"
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "skills" / "demo" / "SKILL.md").write_text("# demo\n")
    (root / "cases.md").write_text("case data\n")
    # codex R1#4:大条目量给 tar|grep 管道加 SIGPIPE 压力(小 fixture 抓不到 -q 早退误判)
    bulk = root / "bulk"
    bulk.mkdir()
    for i in range(1500):
        (bulk / f"f{i:04d}.md").write_text("x\n")
    (base / "env").write_text(
        "EVEROS_API_KEY=super-secret\n"
        f"EVEROS_PROD_ROOT={root}\n"
    )
    dest = tmp_path / "dest"
    envsh = tmp_path / "env.sh"
    envsh.write_text(
        f"EVEROS_PROD_ROOT={root}\n"
        f"EVEROS_BACKUP_DEST={dest}\n"
        "EVEROS_BACKUP_KEEP=7\n"
    )
    return base, dest, envsh


def _stub_tar(tmp_path: pathlib.Path, create_rc: int, touch_output: bool = True) -> pathlib.Path:
    """PATH 前置的 tar stub:create(-czf)调用先跑真 tar 再改写退出码;其余透传。

    touch_output=False 模拟 exit≥2 的"真失败":create 时不产出 archive。
    """
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    stub = bin_dir / "tar"
    if touch_output:
        create_body = f'"{REAL_TAR}" "$@"; real=$?; [ "$real" -ne 0 ] && exit "$real"; exit {create_rc}'
    else:
        create_body = f"exit {create_rc}"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "-czf" ]; then\n'
        f"    {create_body}\n"
        "  fi\n"
        "done\n"
        f'exec "{REAL_TAR}" "$@"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _run(envsh: pathlib.Path, extra_path: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    env = {
        "PATH": (f"{extra_path}:" if extra_path else "") + os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "EVEROS_PROD_ENV": str(envsh),
        "TMPDIR": str(envsh.parent),  # 隔离 lockfile,避免与生产 everos-backup.lock 互斥
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60,
    )


def test_normal_run_publishes_archive(tmp_path: pathlib.Path) -> None:
    """基线守卫:正常路径 exit 0 + archive 落 DEST(改动前后都必须绿)。"""
    _, dest, envsh = _mk_instance(tmp_path)
    r = _run(envsh)
    assert r.returncode == 0, r.stdout + r.stderr
    published = list(dest.glob("everos-*.tar.gz"))
    assert len(published) == 1


def test_tar_exit1_file_changed_is_tolerated(tmp_path: pathlib.Path) -> None:
    """tar exit 1("file changed as we read it")= crash-consistent 设计内警告:
    备份必须照常发布(exit 0 + archive 在 DEST + stdout 有 WARN 行)。"""
    _, dest, envsh = _mk_instance(tmp_path)
    stub_bin = _stub_tar(tmp_path, create_rc=1, touch_output=True)
    r = _run(envsh, extra_path=stub_bin)
    assert r.returncode == 0, f"tar exit 1 不应判死备份:\n{r.stdout}\n{r.stderr}"
    published = list(dest.glob("everos-*.tar.gz"))
    assert len(published) == 1, "exit-1 警告下 archive 应照常发布"
    assert "WARN" in r.stdout, "应打出 WARN 行说明 tar exit 1 被容忍"


def test_tar_exit2_is_fatal_no_publish(tmp_path: pathlib.Path) -> None:
    """tar exit ≥2 = 真错:脚本必须非零退出,DEST 零发布,.tmp 清理。"""
    _, dest, envsh = _mk_instance(tmp_path)
    stub_bin = _stub_tar(tmp_path, create_rc=2, touch_output=False)
    r = _run(envsh, extra_path=stub_bin)
    assert r.returncode != 0
    assert list(dest.glob("everos-*.tar.gz")) == []
    assert list(dest.glob("*.tmp")) == []
