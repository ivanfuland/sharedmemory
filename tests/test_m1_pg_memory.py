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
    _q("DROP TABLE IF EXISTS _m1_probe;")                         # 防旧表残留绕过维度验证
    _q("CREATE TABLE _m1_probe(v vector(1536));")                 # 对齐 text-embedding-3-small
    vec = "[" + ",".join(["0.1"] * 1536) + "]"
    _q(f"INSERT INTO _m1_probe(v) VALUES ('{vec}');")             # 真插 1536 维 → 列宽不符会报错
    assert _q("SELECT vector_dims(v) FROM _m1_probe LIMIT 1;") == "1536"
    _q("DROP TABLE _m1_probe;")
