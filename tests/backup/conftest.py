"""tests/backup 专属夹具：隔离 HOME、合成 data_dir 模板（cp -a 副本）、cass PATH stub、
run_backup 骨架。产物细节见 fixture_factory.py；本文件只装配 pytest 生命周期。"""
from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess
import sys

import pytest

import fixture_factory

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

# infra/backup/cass/ 不是 package，测试文件直接 `import cass_common` 需要它在 sys.path 上。
sys.path.insert(0, str(REPO / "infra" / "backup" / "cass"))


@pytest.fixture
def tmp_home(tmp_path):
    """隔离 HOME：预建 .local/share、NAS 备份目的地、三个会话根
    （.claude/projects、.codex/sessions、.openclaw/agents）。"""
    home = tmp_path / "home"
    (home / ".local" / "share").mkdir(parents=True)
    (home / "nas" / "openclaw" / "backups" / "cass").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".openclaw" / "agents").mkdir(parents=True)
    return home


@pytest.fixture(scope="session")
def _synth_dd_template(tmp_path_factory):
    """session 级只建一次（cass index 要几秒）。函数级测试请用 synth_dd 的 cp -a 副本，
    不要直接改写这份模板——它在整个测试会话内被复用。"""
    home = tmp_path_factory.mktemp("synth-dd-template-home")
    return fixture_factory.build_data_dir(home)


@pytest.fixture
def synth_dd(_synth_dd_template, tmp_path):
    """每个测试函数拿到独立的 data_dir 副本（cp -a，保留属性），可安全改写 / 攻击。"""
    dest = tmp_path / "data_dir"
    subprocess.run(["cp", "-a", str(_synth_dd_template), str(dest)], check=True, timeout=60)
    return dest


@pytest.fixture
def cass_stub(tmp_home):
    """在 PATH 前插一个 cass 的 stub 可执行文件：
    - `cass doctor ...` 且 `$HOME/.cass-stub-doctor.json` 存在 → 原样吐出该文件当 doctor 的
      JSON 输出（跳过真跑几百秒的 doctor，模拟任意 verdict）；
    - 其余情况（含 doctor 但缺 stub 文件）→ exec 真 cass 二进制，行为不变。

    `infra/backup/backup-cass.sh` 要到 Task 9 才落地；本 task 没有测试消费这个 fixture，
    这里只固定接口形状，供后续 task 直接复用。
    """
    real_cass = shutil.which("cass")
    if real_cass is None:
        pytest.skip("cass 不在 PATH，无法搭建 cass_stub")

    stub_dir = tmp_home / ".cass-stub-bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub_path = stub_dir / "cass"
    stub_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'REAL_CASS="{real_cass}"\n'
        'if [ "${1:-}" = "doctor" ] && [ -f "$HOME/.cass-stub-doctor.json" ]; then\n'
        '  cat "$HOME/.cass-stub-doctor.json"\n'
        "  exit 0\n"
        "fi\n"
        'exec "$REAL_CASS" "$@"\n'
    )
    stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub_dir


@pytest.fixture
def run_backup(tmp_home):
    """调 `infra/backup/backup-cass.sh`（Task 9 才落地；本 task 无测试消费，只固定签名）。

    返回一个可调用对象 `run_backup(env=None) -> (rc, out, dest)`；`env` 会与
    `{PATH, HOME}` 白名单合并覆盖（cron 场景的 env 隔离约定，见 spec §11 / V5g3）。
    """
    script = REPO / "infra" / "backup" / "backup-cass.sh"

    def _run(env: dict[str, str] | None = None):
        merged_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_home)}
        if env:
            merged_env.update(env)
        result = subprocess.run(
            ["bash", str(script)],
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        dest = tmp_home / "nas" / "openclaw" / "backups" / "cass"
        return result.returncode, result.stdout + result.stderr, dest

    return _run
