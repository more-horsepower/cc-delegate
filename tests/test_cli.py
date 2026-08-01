"""Tier 1: CLI integration tests.

Runs wh-companion.py as a subprocess with a fake opencode binary that provides
both a mock serve broker and canned NDJSON event streams. Tests the full CLI
command surface: task, status, result, cancel, broker, setup, transfer.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COMPANION = Path(__file__).resolve().parent.parent / "plugins" / "wh-delegate" / "scripts" / "wh-companion.py"


@pytest.fixture
def cli_env(fake_opencode, plugin_data_dir, session_env, tmp_workspace):
    """Full CLI test environment: fake opencode + isolated state + session."""
    yield str(tmp_workspace)
    # Teardown: kill any broker started during the test
    try:
        subprocess.run(
            [sys.executable, str(COMPANION), "broker", "--stop", "--force", "--cwd", str(tmp_workspace)],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass
    # Also try killing any leftover serve processes
    try:
        subprocess.run(["pkill", "-f", "fake.*serve"], capture_output=True, timeout=2)
    except Exception:
        pass


def run_companion(*args, cwd=None, env=None, timeout=30):
    """Run the companion script as a subprocess."""
    full_env = dict(os.environ)
    full_env["PYTHONUNBUFFERED"] = "1"
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(COMPANION), *args],
        capture_output=True, text=True, timeout=timeout,
        env=full_env, cwd=cwd,
    )


class TestTaskForeground:
    def test_basic_task(self, cli_env, ndjson_dir):
        proc = run_companion(
            "task", "test prompt",
            "--cwd", cli_env,
            env={"FAKE_OPENCODE_NDJSON": str(ndjson_dir / "simple_text.ndjson")},
            timeout=15,
        )
        assert proc.returncode == 0
        assert "I'll help you with that." in proc.stdout
        assert "Here is the solution." in proc.stdout

    def test_task_json(self, cli_env, ndjson_dir):
        proc = run_companion(
            "task", "test", "--json",
            "--cwd", cli_env,
            env={"FAKE_OPENCODE_NDJSON": str(ndjson_dir / "simple_text.ndjson")},
            timeout=15,
        )
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["status"] == 0
        assert "threadId" in payload

    def test_task_creates_job(self, cli_env, ndjson_dir):
        run_companion(
            "task", "test", "--cwd", cli_env,
            env={"FAKE_OPENCODE_NDJSON": str(ndjson_dir / "simple_text.ndjson")},
            timeout=15,
        )
        # Check that the job appears in status
        status = run_companion("status", "--json", "--cwd", cli_env, timeout=5)
        assert status.returncode == 0
        payload = json.loads(status.stdout)
        assert payload.get("latestFinished") is not None

    def test_task_with_model(self, cli_env, ndjson_dir):
        proc = run_companion(
            "task", "test", "--model", "mock/custom-model",
            "--cwd", cli_env,
            env={"FAKE_OPENCODE_NDJSON": str(ndjson_dir / "simple_text.ndjson")},
            timeout=15,
        )
        assert proc.returncode == 0

    def test_task_no_prompt_no_resume(self, cli_env):
        proc = run_companion("task", "--cwd", cli_env, timeout=5)
        assert proc.returncode != 0

    def test_task_resume_and_fresh_conflict(self, cli_env):
        proc = run_companion("task", "test", "--resume", "--fresh", "--cwd", cli_env, timeout=5)
        assert proc.returncode != 0
        assert "either" in proc.stderr.lower() or "choose" in proc.stderr.lower()


class TestTaskBackground:
    def test_background_returns_job_id(self, cli_env):
        proc = run_companion(
            "task", "bg task", "--background", "--cwd", cli_env,
            timeout=10,
        )
        assert proc.returncode == 0
        assert "queued" in proc.stdout.lower() or "background" in proc.stdout.lower()

    def test_background_json(self, cli_env):
        proc = run_companion(
            "task", "bg task", "--background", "--json", "--cwd", cli_env,
            timeout=10,
        )
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["status"] == "queued"
        assert "jobId" in payload

    def test_background_appears_in_status(self, cli_env):
        proc = run_companion(
            "task", "bg task", "--background", "--json", "--cwd", cli_env,
            timeout=10,
        )
        job_id = json.loads(proc.stdout)["jobId"]
        status = run_companion("status", "--json", "--cwd", cli_env, timeout=5)
        payload = json.loads(status.stdout)
        running = payload.get("running", [])
        assert any(j["id"] == job_id for j in running)


class TestStatus:
    def test_empty_status(self, cli_env):
        proc = run_companion("status", "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        assert "No jobs" in proc.stdout or "no jobs" in proc.stdout.lower()

    def test_empty_status_json(self, cli_env):
        proc = run_companion("status", "--json", "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["running"] == []
        assert payload["latestFinished"] is None

    def test_status_all(self, cli_env, ndjson_dir):
        # Create a job from current session
        run_companion(
            "task", "task1", "--cwd", cli_env,
            env={"FAKE_OPENCODE_NDJSON": str(ndjson_dir / "simple_text.ndjson")},
            timeout=15,
        )
        # Create a job from another session
        run_companion(
            "task", "task2", "--cwd", cli_env,
            env={
                "FAKE_OPENCODE_NDJSON": str(ndjson_dir / "simple_text.ndjson"),
                "WH_DELEGATE_SESSION_ID": "other-session",
            },
            timeout=15,
        )
        # Without --all, only current session jobs are shown
        proc = run_companion("status", "--json", "--cwd", cli_env, timeout=5)
        payload = json.loads(proc.stdout)
        assert payload["latestFinished"] is not None
        # With --all, both sessions are shown
        proc_all = run_companion("status", "--all", "--json", "--cwd", cli_env, timeout=5)
        payload_all = json.loads(proc_all.stdout)
        assert payload_all["latestFinished"] is not None

    def test_status_by_job_id(self, cli_env, ndjson_dir):
        proc = run_companion(
            "task", "test", "--json", "--cwd", cli_env,
            env={"FAKE_OPENCODE_NDJSON": str(ndjson_dir / "simple_text.ndjson")},
            timeout=15,
        )
        # Get the job ID from status
        status = run_companion("status", "--json", "--cwd", cli_env, timeout=5)
        payload = json.loads(status.stdout)
        job_id = payload["latestFinished"]["id"]
        # Query by job ID
        proc = run_companion("status", job_id, "--json", "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["job"]["id"] == job_id

    def test_status_wait_no_job_id(self, cli_env):
        proc = run_companion("status", "--wait", "--cwd", cli_env, timeout=5)
        assert proc.returncode != 0


class TestResult:
    def test_result_for_completed_job(self, cli_env, ndjson_dir):
        run_companion(
            "task", "test", "--cwd", cli_env,
            env={"FAKE_OPENCODE_NDJSON": str(ndjson_dir / "simple_text.ndjson")},
            timeout=15,
        )
        status = run_companion("status", "--json", "--cwd", cli_env, timeout=5)
        job_id = json.loads(status.stdout)["latestFinished"]["id"]
        proc = run_companion("result", job_id, "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        assert "I'll help you with that." in proc.stdout

    def test_result_json(self, cli_env, ndjson_dir):
        run_companion(
            "task", "test", "--cwd", cli_env,
            env={"FAKE_OPENCODE_NDJSON": str(ndjson_dir / "simple_text.ndjson")},
            timeout=15,
        )
        proc = run_companion("result", "--json", "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert "job" in payload

    def test_result_no_jobs(self, cli_env):
        proc = run_companion("result", "--cwd", cli_env, timeout=5)
        assert proc.returncode != 0

    def test_result_nonexistent_job(self, cli_env):
        proc = run_companion("result", "nonexistent", "--cwd", cli_env, timeout=5)
        assert proc.returncode != 0


class TestCancel:
    def test_cancel_no_active_jobs(self, cli_env):
        proc = run_companion("cancel", "--cwd", cli_env, timeout=5)
        assert proc.returncode != 0
        assert "No active" in proc.stderr

    def test_cancel_background_job(self, cli_env):
        proc = run_companion(
            "task", "bg", "--background", "--json", "--cwd", cli_env,
            timeout=10,
        )
        job_id = json.loads(proc.stdout)["jobId"]
        proc = run_companion("cancel", job_id, "--json", "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["status"] == "cancelled"


class TestBroker:
    def test_broker_status_empty(self, cli_env):
        proc = run_companion("broker", "--json", "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["running"] is False

    def test_broker_status_text(self, cli_env):
        proc = run_companion("broker", "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        assert "not running" in proc.stdout.lower()

    def test_broker_stop_no_broker(self, cli_env):
        proc = run_companion("broker", "--stop", "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        assert "No running broker" in proc.stdout

    def test_broker_started_after_task(self, cli_env, ndjson_dir):
        run_companion(
            "task", "test", "--cwd", cli_env,
            env={"FAKE_OPENCODE_NDJSON": str(ndjson_dir / "simple_text.ndjson")},
            timeout=15,
        )
        proc = run_companion("broker", "--json", "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["running"] is True


class TestSetup:
    def test_setup_json(self, cli_env):
        proc = run_companion("setup", "--json", timeout=10)
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert "ready" in payload
        assert "wh" in payload
        assert "opencode" in payload
        assert payload["opencode"]["available"] is True

    def test_setup_text(self, cli_env):
        proc = run_companion("setup", timeout=10)
        assert proc.returncode == 0
        assert "Workhorse Delegate Setup" in proc.stdout
        assert "opencode" in proc.stdout.lower()


class TestTransfer:
    def test_transfer_basic(self, cli_env, tmp_path, ndjson_dir):
        # Create a fake transcript under ~/.claude/projects/
        projects_dir = Path.home() / ".claude" / "projects" / "test-transfer"
        projects_dir.mkdir(parents=True, exist_ok=True)
        transcript = projects_dir / "session.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"role":"user","content":"Hello"},'
            '"timestamp":"2026-01-01T10:00:00Z"}\n'
        )
        try:
            proc = run_companion(
                "transfer", "--source", str(transcript),
                "--cwd", cli_env, timeout=15,
            )
            assert proc.returncode == 0
            assert "ses_" in proc.stdout
            assert "Imported session" in proc.stdout or "opencode session ID" in proc.stdout
        finally:
            transcript.unlink(missing_ok=True)
            try:
                projects_dir.rmdir()
            except OSError:
                pass

    def test_transfer_nonexistent_source(self, cli_env):
        proc = run_companion(
            "transfer", "--source", "/nonexistent/path.jsonl",
            "--cwd", cli_env, timeout=5,
        )
        assert proc.returncode != 0


class TestTaskResumeCandidate:
    def test_no_resumable(self, cli_env):
        proc = run_companion("task-resume-candidate", "--json", "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["available"] is False

    def test_resumable_after_task(self, cli_env, ndjson_dir):
        run_companion(
            "task", "test", "--cwd", cli_env,
            env={"FAKE_OPENCODE_NDJSON": str(ndjson_dir / "simple_text.ndjson")},
            timeout=15,
        )
        proc = run_companion("task-resume-candidate", "--json", "--cwd", cli_env, timeout=5)
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["available"] is True
        assert "candidate" in payload
        assert payload["candidate"]["threadId"] is not None


class TestUsage:
    def test_usage(self, cli_env):
        proc = run_companion("help", timeout=5)
        assert proc.returncode == 0
        assert "Usage:" in proc.stdout

    def test_unknown_command(self, cli_env):
        proc = run_companion("nonexistent-cmd", "--cwd", cli_env, timeout=5)
        assert proc.returncode != 0
        assert "Unknown" in proc.stderr
