import subprocess
import pytest

PSQL = ["docker", "exec", "pg-memory", "psql", "-U", "gbrain", "-d", "gbrain", "-tA", "-c"]

def _q(sql):
    r = subprocess.run(PSQL + [sql], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"psql 失败（容器没起？）: {r.stderr.strip()}"
    return r.stdout.strip()

def test_gbrain_required_extensions_creatable():
    # GBrain schema.sql 需要这三个（无 zhparser）；gbrain 用户须能建
    for ext in ("vector", "pg_trgm", "pgcrypto"):
        _q(f"CREATE EXTENSION IF NOT EXISTS {ext};")
    have = _q("SELECT extname FROM pg_extension ORDER BY 1;").splitlines()
    assert {"vector", "pg_trgm", "pgcrypto"} <= set(have), f"缺扩展: {have}"

def test_pgvector_1536_column():
    _q("CREATE TABLE IF NOT EXISTS _m1_probe(v vector(1536));")   # 对齐 text-embedding-3-small
    assert _q("SELECT '[1,2,3]'::vector;") == "[1,2,3]"
    _q("DROP TABLE _m1_probe;")
