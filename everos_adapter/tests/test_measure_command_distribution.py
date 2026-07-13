import json
import sqlite3

import msgpack

from scripts.measure_command_distribution import classify, collect, summarize


def _blob(d):
    return msgpack.packb(d, use_bin_type=True)


def _make_db(path):
    """合成 sqlite fixture：schema 只含 collect() 真实 SELECT 的列
    (role, content, extra_bin, extra_json)。行覆盖：
      - call_A：tool_call/tool_result 用 extra_bin(msgpack) 配对，Bash -> pytest
      - call_B：tool_call/tool_result 用 extra_json 回退路径配对，exec_command -> rg
      - call_C：未配对 tool_call（无 result）—— 不应产生任何输出行
      - call_ZZZ：tool_result 找不到对应 tool_call -> 触发 `.get(..., ("", ""))` 元组兜底
    """
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE messages (role TEXT, content TEXT, extra_bin BLOB, extra_json TEXT)")
    rows = [
        ("tool_call", 'Bash({"command":"pytest -x"})',
         _blob({"tool_call_id": "call_A", "tool_call_args": {"command": "pytest -x"}}), None),
        ("tool_result", "A" * 10,
         _blob({"tool_call_id": "call_A"}), None),

        ("tool_call", 'exec_command({"cmd":"rg -n foo"})',
         None, json.dumps({"tool_call_id": "call_B", "tool_call_args": {"cmd": "rg -n foo"}})),
        ("tool_result", "B" * 5,
         None, json.dumps({"tool_call_id": "call_B"})),

        ("tool_call", 'Read({"path":"a.py"})',
         _blob({"tool_call_id": "call_C", "tool_call_args": {"path": "a.py"}}), None),

        ("tool_result", "Z" * 7,
         _blob({"tool_call_id": "call_ZZZ"}), None),
    ]
    con.executemany(
        "INSERT INTO messages (role, content, extra_bin, extra_json) VALUES (?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def test_classify_bash_uses_first_shell_token():
    assert classify("Bash", '{"command":"pytest tests/ -x"}') == "pytest"
    assert classify("bash", '{"cmd":"git diff --stat"}') == "git"
    assert classify("exec_command", '{"cmd":"rg -n foo src/"}') == "rg"


def test_classify_bash_strips_leading_cd_chain():
    assert classify("Bash", '{"command":"cd /tmp && pytest -x"}') == "pytest"


def test_classify_strips_sudo_and_env_without_ampersand():
    # codex R0 P2#7：sudo/env 里没有 &&，原实现会静默回落 tool_name
    assert classify("Bash", '{"command":"sudo pytest -x"}') == "pytest"
    assert classify("Bash", '{"command":"env FOO=1 BAR=2 pytest"}') == "pytest"
    assert classify("Bash", '{"command":"sudo env FOO=1 pytest"}') == "pytest"


def test_classify_strips_wrapper_options_and_bare_assignments():
    # codex R1 P2#4：wrapper 的选项与裸 VAR=VAL 也要跳过
    assert classify("Bash", '{"command":"sudo -E pytest"}') == "pytest"
    assert classify("Bash", '{"command":"env -i pytest"}') == "pytest"
    assert classify("Bash", '{"command":"FOO=1 pytest"}') == "pytest"
    assert classify("Bash", '{"command":"nohup time pytest"}') == "pytest"


def test_classify_bare_prefix_falls_back():
    assert classify("Bash", '{"command":"sudo"}') == "Bash"


def test_classify_structured_tool_uses_tool_name():
    assert classify("Read", '{"path":"a.py"}') == "Read"
    assert classify("update_plan", '{"plan":[]}') == "update_plan"


def test_classify_unparseable_args_falls_back_to_tool_name():
    assert classify("Bash", "not json") == "Bash"
    assert classify("", "") == "<unknown>"


def test_summarize_computes_percentiles():
    rows = [{"command": "pytest", "chars": n} for n in (10, 20, 30, 40, 100)]
    s = summarize(rows)
    assert s["pytest"]["count"] == 5
    assert s["pytest"]["max"] == 100
    assert s["pytest"]["p50"] == 30


def test_collect_pairs_tool_call_and_result_and_classifies(tmp_path):
    # 非空洞性：若配对键错(如误用 idx 而非 tool_call_id)、role 两趟查询写反、
    # extra_json 回退路径漏接、或 `.get(..., ("", ""))` 元组兜底改成别的默认值，
    # 下面任一 assert 都会 RED——这是唯一触达 collect() 真实 SQL join 的测试。
    db_path = tmp_path / "measure.db"
    _make_db(db_path)

    rows = collect(str(db_path))

    # 只有 3 条 tool_result 行；未配对的 call_C（tool_call 无 result）不产生输出行
    assert len(rows) == 3
    got = sorted((r["command"], r["chars"]) for r in rows)
    expected = sorted([
        ("pytest", 10),      # call_A: extra_bin 配对 + Bash -> classify 取 shell 首词
        ("rg", 5),           # call_B: extra_json 回退配对 + exec_command -> classify 取 shell 首词
        ("<unknown>", 7),    # call_ZZZ: 找不到对应 tool_call -> 元组兜底 ("", "") -> classify("", "")
    ])
    assert got == expected


def test_collect_limit_bounds_tool_result_query(tmp_path):
    # --limit 只加在 tool_result 这一趟查询上(见 RUNBOOK 备注)；这里只断言它真的
    # 把输出行数卡住，不假设无 ORDER BY 时的具体行序。
    db_path = tmp_path / "measure_limit.db"
    _make_db(db_path)

    assert len(collect(str(db_path))) == 3
    assert len(collect(str(db_path), limit=2)) == 2
