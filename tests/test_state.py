"""Tier 1: State management tests.

Tests job index, individual job files, logs, leases, and resume candidate
resolution using isolated temp dirs.
"""

import json
from pathlib import Path

import pytest


class TestWorkspaceRoot:
    def test_git_repo(self, companion_module, tmp_workspace):
        root = companion_module.workspace_root(str(tmp_workspace))
        assert Path(root).resolve() == Path(str(tmp_workspace)).resolve()

    def test_non_git_dir(self, companion_module, tmp_path):
        root = companion_module.workspace_root(str(tmp_path))
        assert root == str(tmp_path)


class TestStateDir:
    def test_uses_plugin_data(self, companion_module, tmp_workspace, plugin_data_dir):
        sd = companion_module.state_dir(str(tmp_workspace))
        assert "plugin-data" in str(sd)
        assert "state" in str(sd)

    def test_consistent(self, companion_module, tmp_workspace, plugin_data_dir):
        sd1 = companion_module.state_dir(str(tmp_workspace))
        sd2 = companion_module.state_dir(str(tmp_workspace))
        assert sd1 == sd2

    def test_different_workspaces_different_dirs(self, companion_module, tmp_path, plugin_data_dir):
        w1 = tmp_path / "ws1"
        w2 = tmp_path / "ws2"
        w1.mkdir()
        w2.mkdir()
        for w in [w1, w2]:
            subprocess_run_git_init(w)
        sd1 = companion_module.state_dir(str(w1))
        sd2 = companion_module.state_dir(str(w2))
        assert sd1 != sd2

    def test_slug_from_dirname(self, companion_module, tmp_workspace, plugin_data_dir):
        sd = companion_module.state_dir(str(tmp_workspace))
        # The slug is derived from the directory name
        name = Path(str(tmp_workspace)).name
        assert name in sd.name or sd.name.startswith(name[:3])


def subprocess_run_git_init(path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


class TestJobIndex:
    def test_empty_index(self, companion_module, workspace):
        jobs = companion_module.list_jobs(workspace)
        assert jobs == []

    def test_upsert_insert(self, companion_module, workspace):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "running", "title": "Test task",
            "summary": "A test", "jobClass": "task",
        })
        jobs = companion_module.list_jobs(workspace)
        assert len(jobs) == 1
        assert jobs[0]["id"] == "task-001"
        assert jobs[0]["status"] == "running"
        assert "createdAt" in jobs[0]
        assert "updatedAt" in jobs[0]

    def test_upsert_update(self, companion_module, workspace):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "running", "title": "Test",
        })
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "completed", "title": "Test",
        })
        jobs = companion_module.list_jobs(workspace)
        assert len(jobs) == 1
        assert jobs[0]["status"] == "completed"

    def test_upsert_preserves_fields(self, companion_module, workspace):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "running", "title": "Original",
            "threadId": "ses_123", "jobClass": "task",
        })
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "completed",
        })
        jobs = companion_module.list_jobs(workspace)
        assert jobs[0]["title"] == "Original"
        assert jobs[0]["threadId"] == "ses_123"
        assert jobs[0]["status"] == "completed"

    def test_upsert_ignores_unknown_fields(self, companion_module, workspace):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "running",
            "unknownField": "should not persist",
        })
        jobs = companion_module.list_jobs(workspace)
        assert "unknownField" not in jobs[0]

    def test_upsert_newest_first(self, companion_module, workspace):
        for i in range(5):
            companion_module.upsert_job(workspace, {
                "id": f"task-{i:03d}", "status": "completed", "title": f"Task {i}",
            })
        jobs = companion_module.list_jobs(workspace)
        assert jobs[0]["id"] == "task-004"

    def test_max_jobs_cap(self, companion_module, workspace):
        for i in range(60):
            companion_module.upsert_job(workspace, {
                "id": f"task-{i:03d}", "status": "completed",
            })
        jobs = companion_module.list_jobs(workspace)
        assert len(jobs) == companion_module.MAX_JOBS

    def test_corrupt_index_returns_empty(self, companion_module, workspace):
        sd = companion_module.state_dir(workspace)
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "state.json").write_text("not json at all")
        jobs = companion_module.list_jobs(workspace)
        assert jobs == []


class TestJobFiles:
    def test_write_and_read(self, companion_module, workspace):
        data = {"id": "task-001", "status": "running", "result": {"output": "hello"}}
        companion_module.write_job(workspace, "task-001", data)
        result = companion_module.read_job(workspace, "task-001")
        assert result == data

    def test_read_nonexistent(self, companion_module, workspace):
        result = companion_module.read_job(workspace, "nonexistent")
        assert result is None

    def test_job_file_path(self, companion_module, workspace):
        path = companion_module.job_file(workspace, "task-001")
        assert path.name == "task-001.json"
        assert path.parent == companion_module.jobs_dir(workspace)


class TestAppendLog:
    def test_basic(self, companion_module, workspace):
        companion_module.append_log(workspace, "task-001", "First message")
        companion_module.append_log(workspace, "task-001", "Second message")
        log = companion_module.log_path(workspace, "task-001")
        assert log.exists()
        lines = log.read_text().splitlines()
        assert len(lines) == 2
        assert "First message" in lines[0]
        assert "Second message" in lines[1]

    def test_empty_message_ignored(self, companion_module, workspace):
        companion_module.append_log(workspace, "task-001", "")
        companion_module.append_log(workspace, "task-001", "   ")
        log = companion_module.log_path(workspace, "task-001")
        assert not log.exists() or log.read_text().strip() == ""

    def test_timestamp_prefix(self, companion_module, workspace):
        companion_module.append_log(workspace, "task-001", "test message")
        log = companion_module.log_path(workspace, "task-001")
        line = log.read_text().strip()
        assert line.startswith("[")
        assert "] test message" in line


class TestCurrentSessionJobs:
    def test_filters_by_session(self, companion_module, workspace, session_env):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "completed", "sessionId": session_env,
        })
        companion_module.upsert_job(workspace, {
            "id": "task-002", "status": "completed", "sessionId": "other-session",
        })
        jobs = companion_module.list_jobs(workspace)
        current = companion_module.current_session_jobs(jobs)
        assert len(current) == 1
        assert current[0]["id"] == "task-001"

    def test_no_session_returns_all(self, companion_module, workspace):
        import os
        old = os.environ.pop("WH_DELEGATE_SESSION_ID", None)
        try:
            companion_module.upsert_job(workspace, {"id": "task-001", "status": "completed"})
            companion_module.upsert_job(workspace, {"id": "task-002", "status": "completed",
                                                    "sessionId": "other"})
            jobs = companion_module.list_jobs(workspace)
            current = companion_module.current_session_jobs(jobs)
            assert len(current) == 2
        finally:
            if old:
                os.environ["WH_DELEGATE_SESSION_ID"] = old


class TestResolveResumeCandidate:
    def test_finds_completed_task_with_thread(self, companion_module, workspace, session_env):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "completed", "jobClass": "task",
            "threadId": "ses_123", "sessionId": session_env, "updatedAt": "2026-01-01T10:00:00Z",
        })
        result = companion_module.resolve_resume_candidate(workspace)
        assert result is not None
        assert result["id"] == "task-001"

    def test_skips_running(self, companion_module, workspace, session_env):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "running", "jobClass": "task",
            "threadId": "ses_123", "sessionId": session_env,
        })
        with pytest.raises(SystemExit):
            companion_module.resolve_resume_candidate(workspace)

    def test_skips_queued(self, companion_module, workspace, session_env):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "queued", "jobClass": "task",
            "threadId": "ses_123", "sessionId": session_env,
        })
        with pytest.raises(SystemExit):
            companion_module.resolve_resume_candidate(workspace)

    def test_skips_no_thread_id(self, companion_module, workspace, session_env):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "completed", "jobClass": "task",
            "sessionId": session_env,
        })
        result = companion_module.resolve_resume_candidate(workspace)
        assert result is None

    def test_exclude_job(self, companion_module, workspace, session_env):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "completed", "jobClass": "task",
            "threadId": "ses_123", "sessionId": session_env, "updatedAt": "2026-01-01T10:00:00Z",
        })
        companion_module.upsert_job(workspace, {
            "id": "task-002", "status": "completed", "jobClass": "task",
            "threadId": "ses_456", "sessionId": session_env, "updatedAt": "2026-01-01T11:00:00Z",
        })
        result = companion_module.resolve_resume_candidate(workspace, exclude_job="task-002")
        assert result is not None
        assert result["id"] == "task-001"

    def test_picks_most_recent(self, companion_module, workspace, session_env):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "completed", "jobClass": "task",
            "threadId": "ses_old", "sessionId": session_env, "updatedAt": "2026-01-01T10:00:00Z",
        })
        companion_module.upsert_job(workspace, {
            "id": "task-002", "status": "completed", "jobClass": "task",
            "threadId": "ses_new", "sessionId": session_env, "updatedAt": "2026-01-02T10:00:00Z",
        })
        result = companion_module.resolve_resume_candidate(workspace)
        assert result["id"] == "task-002"


class TestLeases:
    def test_start_and_count(self, companion_module, workspace):
        companion_module.lease_start(workspace, "session-1")
        assert companion_module.lease_count(workspace) == 1

    def test_multiple_leases(self, companion_module, workspace):
        companion_module.lease_start(workspace, "session-1")
        companion_module.lease_start(workspace, "session-2")
        companion_module.lease_start(workspace, "session-3")
        assert companion_module.lease_count(workspace) == 3

    def test_end_reduces_count(self, companion_module, workspace):
        companion_module.lease_start(workspace, "session-1")
        companion_module.lease_start(workspace, "session-2")
        remaining = companion_module.lease_end(workspace, "session-1")
        assert remaining == 1

    def test_end_last_returns_zero(self, companion_module, workspace):
        companion_module.lease_start(workspace, "session-1")
        remaining = companion_module.lease_end(workspace, "session-1")
        assert remaining == 0

    def test_end_nonexistent(self, companion_module, workspace):
        remaining = companion_module.lease_end(workspace, "nonexistent")
        assert remaining == 0

    def test_start_none_sid(self, companion_module, workspace):
        companion_module.lease_start(workspace, None)
        assert companion_module.lease_count(workspace) == 0


class TestMarkCancelled:
    def test_marks_cancelled(self, companion_module, workspace):
        companion_module.write_job(workspace, "task-001", {
            "id": "task-001", "status": "running", "threadId": "ses_123",
            "pid": 12345, "opencodePid": 67890,
        })
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "running", "threadId": "ses_123",
            "pid": 12345, "opencodePid": 67890,
        })
        companion_module._mark_cancelled(workspace, workspace, "task-001", "Test cancel")
        stored = companion_module.read_job(workspace, "task-001")
        assert stored["status"] == "cancelled"
        assert stored["phase"] == "cancelled"
        assert stored["pid"] is None
        assert stored["opencodePid"] is None
        assert stored["errorMessage"] == "Test cancel"
        assert "completedAt" in stored

    def test_preserves_thread_id(self, companion_module, workspace):
        companion_module.write_job(workspace, "task-001", {
            "id": "task-001", "status": "running", "threadId": "ses_123",
        })
        companion_module._mark_cancelled(workspace, workspace, "task-001", "Test cancel")
        stored = companion_module.read_job(workspace, "task-001")
        assert stored["threadId"] == "ses_123"

    def test_updates_index(self, companion_module, workspace):
        companion_module.upsert_job(workspace, {
            "id": "task-001", "status": "running", "threadId": "ses_123",
        })
        companion_module._mark_cancelled(workspace, workspace, "task-001", "Test cancel")
        jobs = companion_module.list_jobs(workspace)
        assert jobs[0]["status"] == "cancelled"
