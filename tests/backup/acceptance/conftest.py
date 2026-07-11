"""Tier B acceptance 专属夹具（task-18）：真实生产对照物 / 真 `cass doctor` / 真
NAS 挂载的接线。跟 `tests/backup/conftest.py`（Tier A，合成夹具）是两条独立赛道，
**不共享 fixture 逻辑**，只共享它已经做好的 `sys.path` 插入（`infra/backup/cass/`
与 `tests/backup/` 本身都已经在 sys.path 上——pytest 导入祖先 conftest.py 的副作用，
`tests/backup/*.py` 全体既有测试都依赖同一套约定，本文件不重复插入）。

两条 gate，都在**收集阶段**统一处理（`pytest_collection_modifyitems`），保证
`pytest tests/backup/acceptance -q` 在缺夹具的机器上产出清楚的 SKIPPED 而不是
collection error 或整批消失：

  1. `CASS_BACKUP_FIXTURES` 未设或目录不存在 → 本目录下全部测试项标记 skip。
  2. 缺该 env 时 `nas` marker 的判断没有意义（整组已经 skip），只在 env 存在时才继续
     判断 `CASS_ACCEPT_NAS=1` 是否给了——没给 → 标了 `nas` marker 的测试项 skip。

PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 偏好 / 基建拓扑 / 真实会话内容。
`CASS_ACCEPT_CORRUPT_BAK` 的默认路径是 CASS 自身的标准 XDG data 目录写法
（`~/.local/share/coding-agent-search/`），不是本机专属基建信息。
"""
from __future__ import annotations

import os
import pathlib
import shutil

import pytest

_ACCEPTANCE_DIR = pathlib.Path(__file__).resolve().parent

_FIXTURES_ENV = "CASS_BACKUP_FIXTURES"
_DEFAULT_FIXTURES_DIR = pathlib.Path.home() / "cass-backup-fixtures"

_CORRUPT_BAK_ENV = "CASS_ACCEPT_CORRUPT_BAK"
_DEFAULT_CORRUPT_BAK = (
    pathlib.Path.home()
    / ".local"
    / "share"
    / "coding-agent-search"
    / "agent_search.db.corrupt-bak-20260709-195904"
)

_NAS_ENV = "CASS_ACCEPT_NAS"


def _fixtures_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get(_FIXTURES_ENV, str(_DEFAULT_FIXTURES_DIR)))


def pytest_collection_modifyitems(config, items):
    """见模块 docstring 的两条 gate。只影响本目录（`tests/backup/acceptance/`）下的
    测试项——本 hook 在全仓任何 conftest 里注册都会对**全部**已收集的 items 触发，
    必须用路径前缀过滤，否则会误伤 Tier A（`tests/backup/*.py`）或仓内其它测试。
    """
    fixtures_dir = _fixtures_dir()
    fixtures_missing = not fixtures_dir.is_dir()
    nas_enabled = os.environ.get(_NAS_ENV) == "1"

    skip_fixtures = pytest.mark.skip(
        reason=(
            f"{_FIXTURES_ENV} 未设或目录不存在（{fixtures_dir}）——"
            "Tier B acceptance 整组 skip，见 tests/backup/acceptance/conftest.py"
        )
    )
    skip_nas = pytest.mark.skip(
        reason=f"{_NAS_ENV}=1 未设——标 nas 的测试 skip（不会碰真实 NAS 挂载）"
    )

    for item in items:
        item_path = pathlib.Path(str(item.fspath)).resolve()
        if not item_path.is_relative_to(_ACCEPTANCE_DIR):
            continue
        if fixtures_missing:
            item.add_marker(skip_fixtures)
            continue
        if "nas" in item.keywords and not nas_enabled:
            item.add_marker(skip_nas)


@pytest.fixture(scope="session")
def fixtures_dir() -> pathlib.Path:
    """`$CASS_BACKUP_FIXTURES`（默认 `~/cass-backup-fixtures/`）。缺失时防御性
    skip——正常情况下 `pytest_collection_modifyitems` 已经在收集阶段拦掉，这里是
    第二道保险（例如某测试直接请求本 fixture 却漏了目录检查的假设变化）。
    """
    d = _fixtures_dir()
    if not d.is_dir():
        pytest.skip(f"{_FIXTURES_ENV} 未设或目录不存在（{d}）")
    return d


@pytest.fixture(scope="session")
def corrupt_bak_path() -> pathlib.Path:
    """生产环境真实留存的损坏库备份（`chattr +i` 只读，`Rowid 905 out of order`
    的对照物，V3）。文件缺失时只 skip 消费它的测试，不牵连整组。"""
    p = pathlib.Path(os.environ.get(_CORRUPT_BAK_ENV, str(_DEFAULT_CORRUPT_BAK)))
    if not p.is_file():
        pytest.skip(f"{_CORRUPT_BAK_ENV} 指向的文件不存在（{p}）")
    return p


@pytest.fixture
def nas_scratch():
    """NAS scratch 目录：`~/nas/openclaw/backups/.cass-accept-<pid>/`。

    要求 `CASS_ACCEPT_NAS=1`（`nas` marker 已经在收集阶段被 skip 掉，本 fixture 的
    检查是防御性第二道——直接请求本 fixture 而漏打 marker 时同样安全，不会意外
    碰真实 NAS）。**绝不**触碰生产 `cass/` 目录：路径写死 `.cass-accept-<pid>` 前缀，
    且显式断言两者互不包含；用后 `finally` 清理，不留现场。
    """
    if os.environ.get(_NAS_ENV) != "1":
        pytest.skip(f"{_NAS_ENV}=1 required for nas_scratch")

    backups_root = pathlib.Path.home() / "nas" / "openclaw" / "backups"
    if not backups_root.is_dir():
        pytest.skip(f"NAS 未挂载或路径不存在：{backups_root}")

    backups_root = backups_root.resolve()
    prod_cass = backups_root / "cass"
    scratch = backups_root / f".cass-accept-{os.getpid()}"

    assert scratch != prod_cass, "scratch 目录不得等于生产 cass/ 目录"
    assert prod_cass not in scratch.parents, "scratch 目录不得位于生产 cass/ 之内"
    assert scratch not in prod_cass.parents, "生产 cass/ 不得位于 scratch 之内（防御性，理论上不可能发生）"

    scratch.mkdir(parents=False, exist_ok=False)
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
