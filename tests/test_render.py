"""Tier 1: Output renderer tests.

Tests _render_job_status, _render_status, _render_result, and _enrich.
"""

import json
from pathlib import Path

import pytest


class TestRenderJobStatus:
    def make_job(self, **overrides):
        base = {"id": "task-001", "status": "running", "kindLabel": "task", "title": "Test Task"}
        base.update(overrides)
        return base

    def test_basic(self, companion_module):
        job = self.make_job()
        out = companion_module._render_job_status(job)
        assert "# Workhorse Delegate Job Status" in out
        assert "task-001" in out
        assert "running" in out
        assert "Test Task" in out

    def test_summary(self, companion_module):
        job = self.make_job(summary="A short summary")
        out = companion_module._render_job_status(job)
        assert "Summary: A short summary" in out

    def test_phase(self, companion_module):
        job = self.make_job(phase="investigating")
        out = companion_module._render_job_status(job)
        assert "Phase: investigating" in out

    def test_thread_id(self, companion_module):
        job = self.make_job(threadId="ses_123", status="completed")
        out = companion_module._render_job_status(job)
        assert "opencode session ID: ses_123" in out
        assert "opencode run --session ses_123" in out

    def test_log_file(self, companion_module):
        job = self.make_job(logFile="/tmp/test.log")
        out = companion_module._render_job_status(job)
        assert "Log: /tmp/test.log" in out

    def test_cancel_action_for_running(self, companion_module):
        job = self.make_job(status="running")
        out = companion_module._render_job_status(job)
        assert "/wh:cancel task-001" in out

    def test_cancel_action_for_queued(self, companion_module):
        job = self.make_job(status="queued")
        out = companion_module._render_job_status(job)
        assert "/wh:cancel task-001" in out

    def test_result_action_for_completed(self, companion_module):
        job = self.make_job(status="completed")
        out = companion_module._render_job_status(job)
        assert "/wh:result task-001" in out

    def test_result_action_for_failed(self, companion_module):
        job = self.make_job(status="failed")
        out = companion_module._render_job_status(job)
        assert "/wh:result task-001" in out

    def test_result_action_for_cancelled(self, companion_module):
        job = self.make_job(status="cancelled")
        out = companion_module._render_job_status(job)
        assert "/wh:result task-001" in out

    def test_elapsed_for_running(self, companion_module):
        job = self.make_job(status="running", elapsed="5s")
        out = companion_module._render_job_status(job)
        assert "Elapsed: 5s" in out

    def test_duration_for_completed(self, companion_module):
        job = self.make_job(status="completed", duration="1m 30s")
        out = companion_module._render_job_status(job)
        assert "Duration: 1m 30s" in out

    def test_progress_preview(self, companion_module):
        job = self.make_job(status="running", progressPreview=["Step 1", "Step 2"])
        out = companion_module._render_job_status(job)
        assert "Progress:" in out
        assert "Step 1" in out
        assert "Step 2" in out


class TestRenderStatus:
    def test_empty(self, companion_module):
        report = {"running": [], "latestFinished": None, "recent": []}
        out = companion_module._render_status(report)
        assert "No jobs recorded yet." in out

    def test_with_running(self, companion_module):
        report = {
            "running": [{"id": "task-001", "status": "running", "kindLabel": "task",
                        "phase": "investigating", "elapsed": "5s", "threadId": "ses_1",
                        "summary": "Working on it"}],
            "latestFinished": None, "recent": [],
        }
        out = companion_module._render_status(report)
        assert "Active jobs:" in out
        assert "task-001" in out
        assert "running" in out
        assert "investigating" in out

    def test_with_latest(self, companion_module):
        report = {
            "running": [],
            "latestFinished": {"id": "task-001", "status": "completed", "summary": "Done",
                               "threadId": "ses_1"},
            "recent": [],
        }
        out = companion_module._render_status(report)
        assert "Latest finished" in out
        assert "task-001" in out
        assert "ses_1" in out

    def test_with_recent(self, companion_module):
        report = {
            "running": [],
            "latestFinished": {"id": "task-001", "status": "completed", "summary": "Done"},
            "recent": [
                {"id": "task-002", "status": "completed", "summary": "Task 2"},
                {"id": "task-003", "status": "failed", "summary": "Task 3"},
            ],
        }
        out = companion_module._render_status(report)
        assert "Recent jobs:" in out
        assert "task-002" in out
        assert "task-003" in out


class TestRenderResult:
    def test_with_raw_output(self, companion_module):
        job = {"id": "task-001", "status": "completed", "title": "Test"}
        stored = {"result": {"rawOutput": "The answer is 42"}, "threadId": "ses_123"}
        out = companion_module._render_result(job, stored)
        assert "The answer is 42" in out
        assert "opencode session ID: ses_123" in out
        assert "opencode run --session ses_123" in out

    def test_raw_output_ensures_newline(self, companion_module):
        job = {"id": "task-001", "status": "completed", "title": "Test"}
        stored = {"result": {"rawOutput": "no newline"}, "threadId": "ses_123"}
        out = companion_module._render_result(job, stored)
        assert out.endswith("\n")

    def test_with_error(self, companion_module):
        job = {"id": "task-001", "status": "failed", "title": "Test", "errorMessage": "Something broke"}
        stored = {"result": {"error": "Something broke"}}
        out = companion_module._render_result(job, stored)
        assert "Something broke" in out

    def test_no_result(self, companion_module):
        job = {"id": "task-001", "status": "completed", "title": "Test Task"}
        stored = {}
        out = companion_module._render_result(job, stored)
        assert "No captured result" in out
        assert "task-001" in out
        assert "Test Task" in out

    def test_error_from_stored(self, companion_module):
        job = {"id": "task-001", "status": "failed", "title": "Test"}
        stored = {"errorMessage": "Stored error", "result": {}}
        out = companion_module._render_result(job, stored)
        assert "Stored error" in out


class TestEnrich:
    def test_adds_kind_label_default(self, companion_module):
        job = {"id": "task-001", "status": "running"}
        result = companion_module._enrich(job)
        assert result["kindLabel"] == "task"

    def test_preserves_kind_label(self, companion_module):
        job = {"id": "task-001", "status": "running", "kindLabel": "rescue"}
        result = companion_module._enrich(job)
        assert result["kindLabel"] == "rescue"

    def test_running_has_elapsed(self, companion_module):
        job = {"id": "task-001", "status": "running", "startedAt": "2026-01-01T10:00:00Z"}
        result = companion_module._enrich(job)
        assert result["elapsed"] is not None

    def test_completed_has_duration(self, companion_module):
        job = {"id": "task-001", "status": "completed",
               "startedAt": "2026-01-01T10:00:00Z", "completedAt": "2026-01-01T10:01:00Z"}
        result = companion_module._enrich(job)
        assert result["duration"] is not None

    def test_running_has_progress_preview(self, companion_module, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("[2026-01-01T10:00:00Z] Step 1\n[2026-01-01T10:00:01Z] Step 2\n")
        job = {"id": "task-001", "status": "running", "logFile": str(log)}
        result = companion_module._enrich(job)
        assert len(result["progressPreview"]) > 0

    def test_completed_no_progress_preview(self, companion_module, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("[2026-01-01T10:00:00Z] Step 1\n")
        job = {"id": "task-001", "status": "completed", "logFile": str(log)}
        result = companion_module._enrich(job)
        assert result["progressPreview"] == []

    def test_no_log_file(self, companion_module):
        job = {"id": "task-001", "status": "running"}
        result = companion_module._enrich(job)
        assert result["progressPreview"] == []
