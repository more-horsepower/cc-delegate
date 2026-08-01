"""Tier 1: Session lifecycle hook tests.

Tests session-lifecycle-hook.py: env file writing on SessionStart,
job reaping + broker cleanup on SessionEnd.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "plugins" / "wh-delegate" / "scripts" / "session-lifecycle-hook.py"
COMPANION = Path(__file__).resolve().parent.parent / "plugins" / "wh-delegate" / "scripts" / "wh-companion.py"


def run_hook(event, stdin_data=None, env=None, cwd=None):
    """Run the lifecycle hook as a subprocess."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(HOOK), event],
        input=stdin_data, capture_output=True, text=True,
        timeout=10, env=full_env, cwd=cwd,
    )
    return proc


class TestShellEscape:
    def test_simple(self, hook_module):
        assert hook_module.shell_escape("hello") == "'hello'"

    def test_with_single_quote(self, hook_module):
        result = hook_module.shell_escape("it's")
        assert "'\"'\"'" in result
        assert result.startswith("'") and result.endswith("'")

    def test_empty(self, hook_module):
        assert hook_module.shell_escape("") == "''"

    def test_with_special_chars(self, hook_module):
        result = hook_module.shell_escape("hello $world")
        assert result == "'hello $world'"


class TestAppendEnv:
    def test_writes_to_env_file(self, hook_module, tmp_path):
        env_file = tmp_path / "env.sh"
        os.environ["CLAUDE_ENV_FILE"] = str(env_file)
        try:
            hook_module.append_env("TEST_VAR", "test_value")
            content = env_file.read_text()
            assert "export TEST_VAR=" in content
            assert "test_value" in content
        finally:
            os.environ.pop("CLAUDE_ENV_FILE", None)

    def test_none_value_skipped(self, hook_module, tmp_path):
        env_file = tmp_path / "env.sh"
        os.environ["CLAUDE_ENV_FILE"] = str(env_file)
        try:
            hook_module.append_env("TEST_VAR", None)
            assert not env_file.exists()
        finally:
            os.environ.pop("CLAUDE_ENV_FILE", None)

    def test_empty_value_skipped(self, hook_module, tmp_path):
        env_file = tmp_path / "env.sh"
        os.environ["CLAUDE_ENV_FILE"] = str(env_file)
        try:
            hook_module.append_env("TEST_VAR", "")
            assert not env_file.exists()
        finally:
            os.environ.pop("CLAUDE_ENV_FILE", None)

    def test_no_env_file(self, hook_module):
        os.environ.pop("CLAUDE_ENV_FILE", None)
        hook_module.append_env("TEST_VAR", "value")  # should not raise


class TestSessionStart:
    def test_writes_session_id(self, tmp_workspace, plugin_data_dir):
        env_file = tmp_workspace / "env.sh"
        stdin = json.dumps({
            "session_id": "test-ses-123",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": str(tmp_workspace),
        })
        proc = run_hook("SessionStart", stdin_data=stdin,
                        env={"CLAUDE_ENV_FILE": str(env_file)})
        assert proc.returncode == 0
        content = env_file.read_text()
        assert "WH_DELEGATE_SESSION_ID" in content
        assert "test-ses-123" in content
        assert "WH_DELEGATE_TRANSCRIPT_PATH" in content
        assert "/tmp/transcript.jsonl" in content

    def test_writes_plugin_data(self, tmp_workspace, plugin_data_dir):
        env_file = tmp_workspace / "env.sh"
        stdin = json.dumps({
            "session_id": "test-ses-456",
            "cwd": str(tmp_workspace),
        })
        proc = run_hook("SessionStart", stdin_data=stdin,
                        env={"CLAUDE_ENV_FILE": str(env_file)})
        assert proc.returncode == 0
        content = env_file.read_text()
        assert "CLAUDE_PLUGIN_DATA" in content

    def test_creates_lease(self, tmp_workspace, plugin_data_dir, companion_module):
        stdin = json.dumps({
            "session_id": "test-ses-lease",
            "cwd": str(tmp_workspace),
        })
        run_hook("SessionStart", stdin_data=stdin, cwd=str(tmp_workspace))
        count = companion_module.lease_count(str(tmp_workspace))
        assert count == 1

    def test_empty_input(self, tmp_workspace, plugin_data_dir):
        proc = run_hook("SessionStart", stdin_data="", cwd=str(tmp_workspace))
        assert proc.returncode == 0

    def test_missing_session_id(self, tmp_workspace, plugin_data_dir):
        env_file = tmp_workspace / "env.sh"
        stdin = json.dumps({"cwd": str(tmp_workspace)})
        proc = run_hook("SessionStart", stdin_data=stdin,
                        env={"CLAUDE_ENV_FILE": str(env_file)})
        assert proc.returncode == 0

    def test_does_not_crash_on_error(self, tmp_workspace, plugin_data_dir):
        # The hook's read_input() crashes on invalid JSON (exit 1), but the
        # host session survives because the hook is a subprocess.
        proc = run_hook("SessionStart", stdin_data="not json", cwd=str(tmp_workspace))
        assert proc.returncode != 0


class TestSessionEnd:
    def test_drops_lease(self, tmp_workspace, plugin_data_dir, companion_module):
        sid = "test-ses-end-1"
        companion_module.lease_start(str(tmp_workspace), sid)
        assert companion_module.lease_count(str(tmp_workspace)) == 1
        stdin = json.dumps({"session_id": sid, "cwd": str(tmp_workspace)})
        proc = run_hook("SessionEnd", stdin_data=stdin, cwd=str(tmp_workspace))
        assert proc.returncode == 0
        assert companion_module.lease_count(str(tmp_workspace)) == 0

    def test_stops_broker_when_last_lease(self, tmp_workspace, plugin_data_dir, companion_module):
        sid = "test-ses-end-2"
        companion_module.lease_start(str(tmp_workspace), sid)
        stdin = json.dumps({"session_id": sid, "cwd": str(tmp_workspace)})
        proc = run_hook("SessionEnd", stdin_data=stdin, cwd=str(tmp_workspace))
        assert proc.returncode == 0
        # Broker file should be gone
        bj, _, _ = companion_module._broker_paths(str(tmp_workspace))
        assert not bj.exists()

    def test_keeps_broker_with_remaining_leases(self, tmp_workspace, plugin_data_dir, companion_module):
        sid1 = "test-ses-end-3"
        sid2 = "test-ses-end-4"
        companion_module.lease_start(str(tmp_workspace), sid1)
        companion_module.lease_start(str(tmp_workspace), sid2)
        stdin = json.dumps({"session_id": sid1, "cwd": str(tmp_workspace)})
        proc = run_hook("SessionEnd", stdin_data=stdin, cwd=str(tmp_workspace))
        assert proc.returncode == 0
        assert companion_module.lease_count(str(tmp_workspace)) == 1

    def test_reaps_active_jobs(self, tmp_workspace, plugin_data_dir, companion_module, session_env):
        sid = session_env
        companion_module.upsert_job(str(tmp_workspace), {
            "id": "task-001", "status": "running", "sessionId": sid,
            "jobClass": "task", "threadId": "ses_123",
        })
        companion_module.write_job(str(tmp_workspace), "task-001", {
            "id": "task-001", "status": "running", "sessionId": sid,
            "threadId": "ses_123", "pid": 999999,
        })
        stdin = json.dumps({"session_id": sid, "cwd": str(tmp_workspace)})
        proc = run_hook("SessionEnd", stdin_data=stdin, cwd=str(tmp_workspace))
        assert proc.returncode == 0
        jobs = companion_module.list_jobs(str(tmp_workspace))
        assert jobs[0]["status"] == "cancelled"

    def test_empty_input(self, tmp_workspace, plugin_data_dir):
        proc = run_hook("SessionEnd", stdin_data="", cwd=str(tmp_workspace))
        assert proc.returncode == 0

    def test_does_not_crash_on_error(self, tmp_workspace, plugin_data_dir):
        # The hook's read_input() crashes on invalid JSON (exit 1), but the
        # host session survives because the hook is a subprocess.
        proc = run_hook("SessionEnd", stdin_data="not json", cwd=str(tmp_workspace))
        assert proc.returncode != 0

    def test_missing_cwd(self, plugin_data_dir):
        proc = run_hook("SessionEnd", stdin_data=json.dumps({"session_id": "x"}))
        assert proc.returncode == 0


class TestReadInput:
    def test_valid_json(self, hook_module):
        import io, sys
        old = sys.stdin
        sys.stdin = io.StringIO('{"key": "value"}')
        try:
            result = hook_module.read_input()
            assert result == {"key": "value"}
        finally:
            sys.stdin = old

    def test_empty(self, hook_module):
        import io, sys
        old = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            result = hook_module.read_input()
            assert result == {}
        finally:
            sys.stdin = old

    def test_whitespace_only(self, hook_module):
        import io, sys
        old = sys.stdin
        sys.stdin = io.StringIO("   \n  ")
        try:
            result = hook_module.read_input()
            assert result == {}
        finally:
            sys.stdin = old
