"""
Task 5 restore smoke test (R4 兜底).

验证 export markdown 可重建检索层：
  export 现库 → 全新临时 PGLite 库 import → 页计数一致 + 页内容 md5 一致。

已知限制（EXIT #9）：
  gbrain import 不保 source_id 分区（单 source 导入）。
  本 smoke 证明「页内容/frontmatter 可重建」，不证 source 分区。
  完整 source 分区恢复需 backup-brain.sh 生成的 pg dump。
"""
import subprocess
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "infra" / "backup" / "restore-smoke.sh"


def test_restore_smoke_counts_match():
    """restore smoke 页计数一致 + 含内容级 md5 比对证据."""
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    stdout = r.stdout
    stderr = r.stderr

    assert r.returncode == 0, (
        f"restore-smoke.sh 非零退出 (rc={r.returncode}):\n"
        f"STDOUT:\n{stdout}\n"
        f"STDERR:\n{stderr[-1000:]}"
    )

    assert "PASS:" in stdout, (
        f"restore smoke 未通过（未见 'PASS:'）:\n{stdout}\n{stderr[-500:]}"
    )

    # 内容级断言：必须含 RESTORE_CONTENT + md5= 作为证据（非仅计数）
    assert "RESTORE_CONTENT" in stdout and "md5=" in stdout, (
        f"须含内容级 md5 比对证据（非仅计数）:\n{stdout}"
    )

    # 计数行必须存在
    assert "RESTORE_SMOKE" in stdout, (
        f"须含 RESTORE_SMOKE 计数行:\n{stdout}"
    )
