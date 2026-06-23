import importlib.util, json, os
import pytest

ENV = ["DISTILL_BASE_URL", "DISTILL_API_KEY", "DISTILL_MODEL"]

@pytest.fixture(scope="module")
def smoke():
    missing = [k for k in ENV if not os.environ.get(k)]
    assert not missing, (f"缺 {missing}。先 `set -a; source infra/distill/config.env; set +a`。"
                         "铁律：API key 非订阅 OAuth。")
    spec = importlib.util.spec_from_file_location("m1_smoke", "infra/distill/smoke.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def test_distill_returns_schema_valid_json(smoke):
    parsed = smoke.distill_once()
    assert parsed["entities"], "未抽到实体"

def test_audit_log_written(smoke, tmp_path):
    log = tmp_path / "audit.log"
    smoke.distill_once(audit_path=str(log))
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["session_ref"] == "SYNTHETIC" and rec["model"] == os.environ["DISTILL_MODEL"]
    assert rec["bytes_out"] > 0 and "ts" in rec
