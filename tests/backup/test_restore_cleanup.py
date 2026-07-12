"""restore-cass.sh 的 EXIT cleanup 决策逻辑测试（codex 2026-07-12 R3/R4 要求的 harness）。

不跑真实 restore——用 mock `systemctl` 跑 cleanup 的**决策语义**，验：
  - `_MCP_SHOULD_RESTART=1` 时任意退出都尝试拉起 cass-mcp（含 rc!=0 的失败退出）；
  - start 后 `is-active` 校验不通过 → 打 WARN 且**把成功 rc(0) 改成非 0**（不谎报成功）；
  - 原始非零 rc 保留；
  - `_MCP_SHOULD_RESTART=0`（进入前 cass-mcp 本就没跑）→ 绝不拉起。

CLEANUP 片段须与 `infra/backup/restore-cass.sh` 的 `cleanup()` **保持一致**（改脚本同步改这里）。
"""
from __future__ import annotations

import subprocess
import textwrap

# —— 与 restore-cass.sh cleanup() 逐字一致（keep in sync）——
_CLEANUP = r"""
cleanup() {
  local rc=$?
  [ -n "$_SHA_TMP" ] && rm -f "$_SHA_TMP" 2>/dev/null || true
  if [ "$_MCP_SHOULD_RESTART" = "1" ]; then
    systemctl --user start cass-mcp 9>&- 8>&- 7>&- 2>/dev/null || true
    if ! systemctl --user is-active --quiet cass-mcp 9>&- 8>&- 7>&-; then
      echo "[restore] WARN: cleanup 未能确认 cass-mcp 已拉起，请手动 systemctl --user start cass-mcp" >&2
      [ "$rc" = "0" ] && rc=1
    fi
  fi
  exec 9>&-
  exec 7>&-
  exit "$rc"
}
"""


def _run(should_restart: str, is_active_after: str, initial_rc: int, start_rc: int = 0):
    """跑一个内嵌 harness：mock systemctl(start 记录调用并按 start_rc 返回 / is-active 按
    is_active_after 返回)，装 cleanup trap，以 initial_rc 退出触发它。**关键**：全局 set -e 下必须
    验证 start 自身非零(start_rc!=0)时后续 is-active/WARN/rc-bump 仍执行。
    返回 (exit_code, start_called: bool, stderr)。"""
    harness = textwrap.dedent(
        f"""
        set -euo pipefail
        _MCP_SHOULD_RESTART={should_restart}
        _SHA_TMP=""
        systemctl() {{
          case "${{2:-}}" in
            start)     echo SYSTEMCTL_START_CALLED ; return {start_rc} ;;  # 打 stdout：真代码对 start
                                                                            # 加了 2>/dev/null（非持久），
                                                                            # 标记走 stderr 会被它吞
            is-active) [ "{is_active_after}" = "yes" ]; return $? ;;
            *)         return 0 ;;
          esac
        }}
        {_CLEANUP}
        trap cleanup EXIT
        exit {initial_rc}
        """
    )
    p = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    return p.returncode, ("SYSTEMCTL_START_CALLED" in p.stdout), p.stderr


def test_should_restart_start_ok_preserves_success_rc():
    rc, start_called, _ = _run(should_restart="1", is_active_after="yes", initial_rc=0)
    assert start_called is True
    assert rc == 0


def test_should_restart_but_start_fails_bumps_success_to_nonzero():
    # 成功 restore(rc=0) 但 cass-mcp 没回来 → 必须报非 0，不谎报成功
    rc, start_called, err = _run(should_restart="1", is_active_after="no", initial_rc=0)
    assert start_called is True
    assert rc != 0
    assert "未能确认 cass-mcp 已拉起" in err


def test_failure_rc_preserved_and_restart_attempted():
    # 中途 FATAL(rc=1) 也必须尝试拉起 cass-mcp，且保留原始非零 rc
    rc, start_called, _ = _run(should_restart="1", is_active_after="yes", initial_rc=1)
    assert start_called is True
    assert rc == 1


def test_not_active_before_never_restarts():
    # 进入脚本前 cass-mcp 本就没跑 → 绝不拉起（避免起了个本不该起的服务）
    rc, start_called, _ = _run(should_restart="0", is_active_after="no", initial_rc=0)
    assert start_called is False
    assert rc == 0


def test_start_command_itself_nonzero_still_verifies_and_warns():
    # 全局 set -e 下，start 自身返回非零绝不能打断 trap → is-active/WARN/rc-bump 仍必须执行
    # （codex R5 抓出：缺 `|| true` 会在 WARN 前 errexit，服务留停且无提示，rc 被覆盖）
    rc, start_called, err = _run(
        should_restart="1", is_active_after="no", initial_rc=0, start_rc=1
    )
    assert start_called is True
    assert rc != 0
    assert "未能确认 cass-mcp 已拉起" in err


def test_start_nonzero_but_service_actually_up_passes():
    # start 返回非零但服务其实起来了（is-active=yes）→ 通过（判据是 is-active，不是 start rc）
    rc, start_called, _ = _run(
        should_restart="1", is_active_after="yes", initial_rc=0, start_rc=1
    )
    assert start_called is True
    assert rc == 0
