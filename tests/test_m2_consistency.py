"""test_m2_consistency.py — P2 正例：三端同问一实体答案一致。

★ 三端真实 adapter 脚本（cc_sessionstart.sh / codex_sessionstart.sh / openclaw_bootstrap.sh）
  对同一 seed 实体查询 → top [[slug]] 必须三端一致且命中种子页。

设计要点：
- GBRAIN_DIGEST_THRESHOLD=0.0 via subprocess env，确保种子页在近空 brain 也必中
  （真实 brain 已有内容时 0.75 阈值也能过；env override 在两种场景下均保险）。
- CODEX_AGENTS_FILE / OPENCLAW_AGENTS_FILE 指向 tmp 文件，绝不触碰
  ~/.codex/AGENTS.md 或 ~/.openclaw/agents/*/AGENTS.md 等宿主文件。
- CC adapter 输出 JSON 到 stdout，不写任何文件。
- 任一 adapter 脚本缺失 → FAIL（非 skip）——三端 gate 要求三个脚本都在。
"""
import json
import os
import re
import subprocess
import tempfile
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SLUG_RE = re.compile(r"\[\[([^\]]+)\]\]")

# 三端 adapter 入口 + 各自的 workspace 信号 env
# codex/openclaw 注入到 temp 文件（不碰宿主真实 AGENTS.md）
ADAPTERS = [
    (
        "cc",
        ROOT / "hooks/cc_sessionstart.sh",
        {"CLAUDE_PROJECT_DIR": "/home/ivan/projects/sharedmemory"},
        None,  # cc 无目标文件（输出 JSON to stdout）
    ),
    (
        "codex",
        ROOT / "hooks/codex_sessionstart.sh",
        {"CODEX_PROJECT_DIR": "/home/ivan/projects/sharedmemory"},
        "CODEX_AGENTS_FILE",  # env var 名，测试时指向 tmp
    ),
    (
        "openclaw",
        ROOT / "hooks/openclaw_bootstrap.sh",
        {"OPENCLAW_WORKSPACE": "sharedmemory"},
        "OPENCLAW_AGENTS_FILE",  # env var 名，测试时指向 tmp
    ),
]


def _seed_page() -> str:
    """put + embed 已知 slug，返回预期 top slug。"""
    env = {
        **os.environ,
        "GBRAIN_HOME": str(ROOT / "sandbox/gbrain-pg"),
        "PATH": os.path.expanduser("~/.bun/bin") + ":" + os.environ.get("PATH", ""),
    }
    # put（幂等：已存在则 update）
    r_put = subprocess.run(
        ["gbrain", "put", "projects/sharedmemory",
         "--content", "# 共享记忆层\n架构与决策结论锚点页"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert r_put.returncode == 0, f"gbrain put 失败: {r_put.stderr[:200]}"

    # embed（幂等：已嵌入则 skip）
    r_emb = subprocess.run(
        ["gbrain", "embed", "projects/sharedmemory"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert r_emb.returncode == 0, f"gbrain embed 失败: {r_emb.stderr[:200]}"

    return "projects/sharedmemory"


def _top_slug_from_adapter(
    name: str,
    script: pathlib.Path,
    env_extra: dict,
    file_env_var: str | None,
) -> str | None:
    """真跑 adapter 脚本，抽取注入文本里的第一个 [[slug]]。

    - GBRAIN_DIGEST_THRESHOLD=0.0：确保种子页在近空 brain 也必中（env override）。
    - codex/openclaw 的目标文件指向 tmp，不碰宿主真实 AGENTS.md。
    """
    env = {
        **os.environ,
        "GBRAIN_HOME": str(ROOT / "sandbox/gbrain-pg"),
        "PATH": os.path.expanduser("~/.bun/bin") + ":" + os.environ.get("PATH", ""),
        "GBRAIN_DIGEST_THRESHOLD": "0.0",  # 阈值 override：种子页必中（宁注多勿漏）
        **env_extra,
    }

    # codex/openclaw：临时文件隔离，绝不碰宿主真实 AGENTS.md
    tmp_file = None
    if file_env_var:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".md", prefix=f"test-{name}-agents-")
        os.close(tmp_fd)
        tmp_file = tmp_path
        env[file_env_var] = tmp_path

    try:
        r = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=60, env=env,
        )
        assert r.returncode == 0, (
            f"{script.name} 必须 exit 0，实际 rc={r.returncode}: {r.stderr[:300]}"
        )

        # cc：从 JSON hookSpecificOutput.additionalContext 里搜 slug
        # codex/openclaw：从 stdout 直接搜 slug
        text_to_search = r.stdout
        if name == "cc":
            try:
                obj = json.loads(r.stdout)
                text_to_search = (
                    obj.get("hookSpecificOutput", {}).get("additionalContext", "")
                    or r.stdout
                )
            except Exception:
                text_to_search = r.stdout

        m = SLUG_RE.search(text_to_search)
        return m.group(1) if m else None

    finally:
        if tmp_file and os.path.exists(tmp_file):
            os.unlink(tmp_file)
        # 也清 .bak（codex/openclaw 幂等备份）
        if tmp_file and os.path.exists(tmp_file + ".gbrain-digest.bak"):
            os.unlink(tmp_file + ".gbrain-digest.bak")


def test_three_harness_same_entity_consistent():
    """★ P2 正例：三端三个真实 adapter（非同一 CLI 三遍）对同一 seed 实体 → top slug 三端一致。

    缺任一 adapter 脚本 = FAIL（不 skip 假绿）——三端 gate 要求三个脚本都在。

    验收标准（codex R1 #7 / R2 #3）：
    1. 三端 adapter 脚本全部存在。
    2. 每端都注入到至少一个 [[slug]]（含 seed 实体）。
    3. 三端 top slug 完全一致（== 同一字符串）。
    4. top slug 命中 seed 实体（"projects/sharedmemory"）。
    """
    # 前置：seed 已知实体并 embed
    expected_slug = _seed_page()

    # 检查三端脚本存在（FAIL 不 skip）
    missing = [
        name
        for name, script, _, _ in ADAPTERS
        if not script.exists()
    ]
    assert not missing, (
        f"三端 gate：adapter 脚本缺失 {missing}——"
        "Task3 须产齐三端脚本，缺失不得 skip 当绿"
    )

    # 跑三端 adapter，收集 top slug
    tops: dict[str, str | None] = {}
    for name, script, env_extra, file_env_var in ADAPTERS:
        tops[name] = _top_slug_from_adapter(name, script, env_extra, file_env_var)

    # 断言 1：每端都有注入（含 seed 实体）
    assert all(tops.values()), (
        f"每端都须注入到 [[slug]]（含 seed 实体）: {tops}"
    )

    # 断言 2：三端 top slug 完全一致
    assert len(set(tops.values())) == 1, (
        f"三端真 adapter top slug 不一致: {tops}"
    )

    # 断言 3：top slug 命中 seed 实体
    assert expected_slug in set(tops.values()), (
        f"top 应命中 seed 实体 '{expected_slug}'，实际: {tops}"
    )
