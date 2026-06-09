"""全局测试夹具：
- 给 GBRAIN_HOME 设默认值（沙盒目录），使 GBrain 测试不因未设 env 而 skip；
- 标 needs_gbrain 的测试在沙盒缺失时 fail（非 skip）——GBrain 核心门必跑。
只有"读端路线未启用"（canonical/fallback 互斥）允许 skip。"""
import os
import pathlib
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
os.environ.setdefault("GBRAIN_HOME", str(REPO / "sandbox" / "gbrain"))


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_gbrain: 需要 GBrain 沙盒，缺失即 fail 不 skip")


@pytest.fixture(autouse=True)
def _gbrain_guard(request):
    if request.node.get_closest_marker("needs_gbrain"):
        home = pathlib.Path(os.environ["GBRAIN_HOME"])
        if not home.is_dir():
            pytest.fail(f"GBrain 沙盒缺失 {home}——先跑 Task 4 init（GBrain 门不许 skip）")
