# tests/test_m4_sample_cass.py
import sqlite3
from distill import sample_cass


def _mk(p):
    db = sqlite3.connect(p)
    db.executescript("""CREATE TABLE agents(id INTEGER PRIMARY KEY,slug TEXT);
      CREATE TABLE workspaces(id INTEGER PRIMARY KEY,path TEXT);
      CREATE TABLE conversations(id INTEGER PRIMARY KEY,agent_id INT,workspace_id INT,source_path TEXT);
      CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id INT,idx INT,role TEXT,created_at INT,content TEXT);
      INSERT INTO agents VALUES(1,'claude_code'),(2,'codex');
      INSERT INTO conversations VALUES(10,1,NULL,'/cc/a'),(20,2,NULL,'/codex/b');""")
    db.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?)",
      [(1,10,0,'user',0,'老兰决定 xagent 用 TypeScript + Next.js 15'),(2,10,1,'assistant',0,'HEARTBEAT_OK'),
       (3,20,0,'user',0,'Portola native 层只承载平台事实，禁产品决策，架构红线')])
    db.commit()
    db.close()


def test_pool_shape_noise_cluster(tmp_path):
    p = str(tmp_path / "c.db")
    _mk(p)
    pool = sample_cass.sample_pool(p, pool_size=10, seed=1, min_chars=5)
    for s in pool:
        assert s["split"] == "real" and s["cluster"] in ("claude_code", "codex")
        assert all(r["content"] != "HEARTBEAT_OK" for r in s["span"]) and "_meta" in s


def test_reproducible(tmp_path):
    p = str(tmp_path / "c.db")
    _mk(p)
    assert sample_cass.sample_pool(p, pool_size=10, seed=7, min_chars=5) == sample_cass.sample_pool(p, pool_size=10, seed=7, min_chars=5)
