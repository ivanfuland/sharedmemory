"""
Task 6: Protocol wiring tests (deferred-activation scope).

What this tests:
  1. Protocol files exist and contain required key phrases.
  2. Codex adapter targets AGENTS.md (not memories/).
  3. Subprocess fail-soft for codex adapter:
     - normal run (GBRAIN_HOME unreachable) → exit 0 + idempotent prepend into temp file
     - run twice → exactly one digest block (idempotent)
  4. Subprocess fail-soft for openclaw adapter:
     - normal run (GBRAIN_HOME unreachable) → exit 0 + idempotent prepend into temp file
     - run twice → exactly one digest block (idempotent)
  5. Malformed GBRAIN_HOME env → exit 0 (no crash).

What this does NOT test:
  - Live host file wiring (settings.json / real AGENTS.md) — that is the activation checklist (Task 8).
"""

import os
import pathlib
import re
import subprocess
import tempfile

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent

CODEX_ADAPTER = REPO_ROOT / "hooks" / "codex_sessionstart.sh"
OPENCLAW_ADAPTER = REPO_ROOT / "hooks" / "openclaw_bootstrap.sh"
LOOKUP_PROTOCOL = REPO_ROOT / "protocols" / "lookup-protocol.md"
TRIAGE_PROTOCOL = REPO_ROOT / "protocols" / "openclaw-triage.md"

INJECT_BEGIN = "<!-- gbrain-digest:begin -->"
INJECT_END = "<!-- gbrain-digest:end -->"

# ---------------------------------------------------------------------------
# 1. Protocol file existence + content
# ---------------------------------------------------------------------------

def test_lookup_protocol_exists():
    assert LOOKUP_PROTOCOL.exists(), "protocols/lookup-protocol.md missing"
    text = LOOKUP_PROTOCOL.read_text(encoding="utf-8")
    assert "首中即停" in text, "lookup-protocol.md missing '首中即停'"
    assert "CASS" in text, "lookup-protocol.md missing CASS"
    assert "之前" in text or "上次" in text, "lookup-protocol.md missing '之前/上次'"
    assert "stale" in text, "lookup-protocol.md missing stale marker guidance"


def test_triage_protocol_exists():
    assert TRIAGE_PROTOCOL.exists(), "protocols/openclaw-triage.md missing"
    text = TRIAGE_PROTOCOL.read_text(encoding="utf-8")
    assert "gbrain" in text, "triage protocol missing gbrain reference"
    assert "daily memory" in text, "triage protocol missing daily memory"
    assert "跨会话" in text or "换个会话" in text, "triage protocol missing session-stability criterion"


# ---------------------------------------------------------------------------
# 2. Codex adapter script targets AGENTS.md, NOT memories/
# ---------------------------------------------------------------------------

def test_codex_adapter_targets_agents_md_not_memories():
    text = CODEX_ADAPTER.read_text(encoding="utf-8")
    assert "AGENTS.md" in text, "codex_sessionstart.sh does not reference AGENTS.md"
    # Must not *write* to memories/ path — check the Python section has no write to memories/
    # (comments may mention it for historical context; the operative code must not use it)
    assert "memories_dir" not in text, (
        "codex_sessionstart.sh still defines memories_dir — old write-to-memories/ code present"
    )
    # The operative target must be AGENTS.md, not a memories/ path construction
    assert '".codex/memories"' not in text and "/.codex/memories" not in text, (
        "codex_sessionstart.sh still constructs a memories/ path — must target AGENTS.md instead"
    )


def test_codex_adapter_has_path_override():
    text = CODEX_ADAPTER.read_text(encoding="utf-8")
    assert "CODEX_AGENTS_FILE" in text, "codex_sessionstart.sh missing CODEX_AGENTS_FILE env-override"


def test_openclaw_adapter_has_path_override():
    text = OPENCLAW_ADAPTER.read_text(encoding="utf-8")
    assert "OPENCLAW_AGENTS_FILE" in text, "openclaw_bootstrap.sh missing OPENCLAW_AGENTS_FILE env-override"


# ---------------------------------------------------------------------------
# Helpers for subprocess adapter tests
# ---------------------------------------------------------------------------

def _count_digest_blocks(content: str) -> int:
    """Count how many complete gbrain-digest begin/end block pairs exist."""
    return len(re.findall(re.escape(INJECT_BEGIN), content))


def _run_adapter(script: pathlib.Path, env: dict, timeout: int = 30) -> subprocess.CompletedProcess:
    env_full = {**os.environ, **env}
    return subprocess.run(
        ["bash", str(script)],
        env=env_full,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 3. Codex adapter subprocess tests
# ---------------------------------------------------------------------------

class TestCodexAdapterSubprocess:
    """Run codex_sessionstart.sh in subprocess with GBRAIN_HOME=/nonexistent and a temp AGENTS.md."""

    def _run(self, agents_file: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {
            "GBRAIN_HOME": "/nonexistent_gbrain_home_m2test",
            "CODEX_AGENTS_FILE": agents_file,
        }
        if extra_env:
            env.update(extra_env)
        return _run_adapter(CODEX_ADAPTER, env)

    def test_exit_zero_unreachable_gbrain(self, tmp_path):
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text("# Existing content\n", encoding="utf-8")
        result = self._run(str(agents_file))
        assert result.returncode == 0, (
            f"codex adapter exited {result.returncode}\nstderr: {result.stderr}"
        )

    def test_prepend_to_existing_file(self, tmp_path):
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text("# Existing content\n", encoding="utf-8")
        result = self._run(str(agents_file))
        assert result.returncode == 0
        content = agents_file.read_text(encoding="utf-8")
        # Should have exactly one digest block prepended
        assert INJECT_BEGIN in content, "digest block begin marker missing"
        assert INJECT_END in content, "digest block end marker missing"
        assert _count_digest_blocks(content) == 1

    def test_idempotent_double_run(self, tmp_path):
        """Running twice must produce exactly one digest block (idempotent prepend)."""
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text("# Existing content\n", encoding="utf-8")
        r1 = self._run(str(agents_file))
        r2 = self._run(str(agents_file))
        assert r1.returncode == 0
        assert r2.returncode == 0
        content = agents_file.read_text(encoding="utf-8")
        count = _count_digest_blocks(content)
        assert count == 1, f"Expected 1 digest block after double run, got {count}"

    def test_exit_zero_no_file(self, tmp_path):
        """File doesn't exist yet — adapter should still exit 0."""
        agents_file = tmp_path / "AGENTS_new.md"
        result = self._run(str(agents_file))
        assert result.returncode == 0

    def test_exit_zero_malformed_env(self, tmp_path):
        """Malformed/unusual GBRAIN_HOME must not crash the adapter."""
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text("", encoding="utf-8")
        result = self._run(str(agents_file), extra_env={"GBRAIN_HOME": "/dev/null"})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# 4. OpenClaw adapter subprocess tests
# ---------------------------------------------------------------------------

class TestOpenClawAdapterSubprocess:
    """Run openclaw_bootstrap.sh in subprocess with GBRAIN_HOME=/nonexistent and a temp AGENTS.md."""

    def _run(self, agents_file: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {
            "GBRAIN_HOME": "/nonexistent_gbrain_home_m2test",
            "OPENCLAW_AGENTS_FILE": agents_file,
        }
        if extra_env:
            env.update(extra_env)
        return _run_adapter(OPENCLAW_ADAPTER, env)

    def test_exit_zero_unreachable_gbrain(self, tmp_path):
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text("# Existing content\n", encoding="utf-8")
        result = self._run(str(agents_file))
        assert result.returncode == 0, (
            f"openclaw adapter exited {result.returncode}\nstderr: {result.stderr}"
        )

    def test_prepend_to_existing_file(self, tmp_path):
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text("# Agent instructions\n", encoding="utf-8")
        result = self._run(str(agents_file))
        assert result.returncode == 0
        content = agents_file.read_text(encoding="utf-8")
        assert INJECT_BEGIN in content, "digest block begin marker missing"
        assert INJECT_END in content, "digest block end marker missing"
        assert _count_digest_blocks(content) == 1

    def test_idempotent_double_run(self, tmp_path):
        """Running twice must produce exactly one digest block (idempotent prepend)."""
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text("# Agent instructions\n", encoding="utf-8")
        r1 = self._run(str(agents_file))
        r2 = self._run(str(agents_file))
        assert r1.returncode == 0
        assert r2.returncode == 0
        content = agents_file.read_text(encoding="utf-8")
        count = _count_digest_blocks(content)
        assert count == 1, f"Expected 1 digest block after double run, got {count}"

    def test_exit_zero_malformed_env(self, tmp_path):
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text("", encoding="utf-8")
        result = self._run(str(agents_file), extra_env={"GBRAIN_HOME": "/dev/null"})
        assert result.returncode == 0

    def test_original_content_preserved(self, tmp_path):
        """Original content should still appear after prepend."""
        original_marker = "ORIGINAL_CONTENT_MARKER_XYZ"
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text(f"# Original\n{original_marker}\n", encoding="utf-8")
        self._run(str(agents_file))
        content = agents_file.read_text(encoding="utf-8")
        assert original_marker in content, "Original content was lost after digest prepend"


# ---------------------------------------------------------------------------
# 5. Sanity: no live host files were touched
# ---------------------------------------------------------------------------

def test_live_codex_agents_md_not_touched():
    """The real ~/.codex/AGENTS.md must NOT be modified by our test suite."""
    real = pathlib.Path.home() / ".codex" / "AGENTS.md"
    if not real.exists():
        pytest.skip("~/.codex/AGENTS.md does not exist on this machine")
    # Check that no .gbrain-digest.bak was created beside it (would indicate test wrote to it)
    bak = pathlib.Path(str(real) + ".gbrain-digest.bak")
    # We only assert the bak doesn't exist if AGENTS.md has no digest block already
    # (it could exist from a previous legitimate manual run)
    content = real.read_text(encoding="utf-8")
    if INJECT_BEGIN not in content:
        assert not bak.exists(), (
            "~/.codex/AGENTS.md.gbrain-digest.bak exists unexpectedly — "
            "test may have written to the real file"
        )
