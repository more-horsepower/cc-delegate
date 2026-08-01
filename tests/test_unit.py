"""Tier 1: Pure function unit tests.

No external dependencies, no filesystem, no network.
"""

import time
from datetime import datetime

import pytest


class TestParseVersion:
    def test_simple(self, companion_module):
        assert companion_module.parse_version("1.2.3") == (1, 2, 3)

    def test_with_label(self, companion_module):
        assert companion_module.parse_version("opencode 1.18.11") == (1, 18, 11)

    def test_two_parts(self, companion_module):
        assert companion_module.parse_version("2.0") == (2, 0)

    def test_empty(self, companion_module):
        assert companion_module.parse_version("") == (0,)

    def test_none(self, companion_module):
        assert companion_module.parse_version(None) == (0,)

    def test_no_digits(self, companion_module):
        assert companion_module.parse_version("no version here") == (0,)

    def test_more_than_three(self, companion_module):
        assert companion_module.parse_version("1.2.3.4.5") == (1, 2, 3)

    def test_sorting(self, companion_module):
        versions = [
            companion_module.parse_version("1.0.0"),
            companion_module.parse_version("2.0.0"),
            companion_module.parse_version("1.18.11"),
            companion_module.parse_version("1.2.3"),
        ]
        versions.sort(reverse=True)
        assert versions[0] == (2, 0, 0)
        assert versions[1] == (1, 18, 11)


class TestShorten:
    def test_short(self, companion_module):
        assert companion_module.shorten("hello") == "hello"

    def test_exact_limit(self, companion_module):
        text = "a" * 96
        assert companion_module.shorten(text) == text

    def test_truncate(self, companion_module):
        text = "a" * 100
        result = companion_module.shorten(text)
        assert len(result) == 96
        assert result.endswith("...")

    def test_custom_limit(self, companion_module):
        result = companion_module.shorten("hello world", limit=5)
        assert result == "he..."

    def test_collapses_whitespace(self, companion_module):
        assert companion_module.shorten("  hello   world  ") == "hello world"

    def test_none(self, companion_module):
        assert companion_module.shorten(None) == ""

    def test_empty(self, companion_module):
        assert companion_module.shorten("") == ""


class TestFirstLine:
    def test_simple(self, companion_module):
        assert companion_module.first_line("hello\nworld", "fallback") == "hello"

    def test_leading_whitespace(self, companion_module):
        assert companion_module.first_line("\n\n  hello\nworld", "fallback") == "hello"

    def test_empty(self, companion_module):
        assert companion_module.first_line("", "fallback") == "fallback"

    def test_none(self, companion_module):
        assert companion_module.first_line(None, "fallback") == "fallback"

    def test_only_whitespace(self, companion_module):
        assert companion_module.first_line("  \n  \n", "fallback") == "fallback"


class TestNowIso:
    def test_format(self, companion_module):
        ts = companion_module.now_iso()
        assert ts.endswith("Z")
        assert "T" in ts
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


class TestNowMs:
    def test_reasonable(self, companion_module):
        before = int(time.time() * 1000)
        result = companion_module.now_ms()
        after = int(time.time() * 1000)
        assert before <= result <= after


class TestToMs:
    def test_none_returns_now(self, companion_module):
        before = companion_module.now_ms()
        result = companion_module._to_ms(None)
        after = companion_module.now_ms()
        assert before <= result <= after

    def test_int(self, companion_module):
        assert companion_module._to_ms(12345) == 12345

    def test_float(self, companion_module):
        assert companion_module._to_ms(12345.67) == 12345

    def test_iso_string(self, companion_module):
        result = companion_module._to_ms("2026-01-01T10:00:00Z")
        expected = int(datetime.fromisoformat("2026-01-01T10:00:00+00:00").timestamp() * 1000)
        assert result == expected

    def test_invalid_string(self, companion_module):
        before = companion_module.now_ms()
        result = companion_module._to_ms("not a timestamp")
        after = companion_module.now_ms()
        assert before <= result <= after


class TestNewJobId:
    def test_prefix(self, companion_module):
        jid = companion_module.new_job_id("task")
        assert jid.startswith("task-")

    def test_custom_prefix(self, companion_module):
        jid = companion_module.new_job_id("rescue")
        assert jid.startswith("rescue-")

    def test_default_prefix(self, companion_module):
        jid = companion_module.new_job_id()
        assert jid.startswith("task-")

    def test_unique(self, companion_module):
        ids = {companion_module.new_job_id() for _ in range(100)}
        assert len(ids) == 100

    def test_hex_suffix(self, companion_module):
        jid = companion_module.new_job_id("task")
        parts = jid.split("-")
        assert len(parts) == 3
        assert len(parts[2]) == 6


class TestDescribeTool:
    def test_running(self, companion_module):
        result = companion_module._describe_tool({"tool": "read", "state": {"status": "running", "title": "Reading file"}})
        assert result == "Running tool: read — Reading file"

    def test_running_no_title(self, companion_module):
        result = companion_module._describe_tool({"tool": "bash", "state": {"status": "running"}})
        assert result == "Running tool: bash"

    def test_completed(self, companion_module):
        result = companion_module._describe_tool({"tool": "read", "state": {"status": "completed", "title": "Reading file"}})
        assert result == "Tool completed: read — Reading file"

    def test_error(self, companion_module):
        result = companion_module._describe_tool({"tool": "write", "state": {"status": "error"}})
        assert result == "Tool failed: write"

    def test_no_state(self, companion_module):
        result = companion_module._describe_tool({"tool": "read"})
        assert result == "Running tool: read"

    def test_empty_part(self, companion_module):
        result = companion_module._describe_tool({})
        assert result == "Running tool: tool"

    def test_none_state(self, companion_module):
        result = companion_module._describe_tool({"tool": "bash", "state": None})
        assert result == "Running tool: bash"


class TestTaskMetadata:
    def test_task(self, companion_module):
        meta = companion_module.task_metadata("Fix the bug", resume=False)
        assert meta["title"] == "opencode Task"
        assert meta["summary"] == "Fix the bug"

    def test_resume(self, companion_module):
        meta = companion_module.task_metadata("Continue", resume=True)
        assert meta["title"] == "opencode Resume"
        assert meta["summary"] == "Continue"

    def test_resume_no_prompt(self, companion_module):
        meta = companion_module.task_metadata("", resume=True)
        assert meta["title"] == "opencode Resume"
        assert meta["summary"] == companion_module.DEFAULT_CONTINUE_PROMPT

    def test_task_no_prompt(self, companion_module):
        meta = companion_module.task_metadata("", resume=False)
        assert meta["title"] == "opencode Task"
        assert meta["summary"] == "Task"

    def test_long_prompt(self, companion_module):
        long_prompt = "x" * 200
        meta = companion_module.task_metadata(long_prompt, resume=False)
        assert len(meta["summary"]) == 96
        assert meta["summary"].endswith("...")


class TestFindJob:
    def make_jobs(self):
        return [
            {"id": "task-aaa-001", "status": "completed"},
            {"id": "task-bbb-002", "status": "running"},
            {"id": "task-ccc-003", "status": "failed"},
        ]

    def test_exact_id(self, companion_module):
        jobs = self.make_jobs()
        result = companion_module._find_job(jobs, "task-aaa-001")
        assert result["id"] == "task-aaa-001"

    def test_prefix_unique(self, companion_module):
        jobs = self.make_jobs()
        result = companion_module._find_job(jobs, "task-a")
        assert result["id"] == "task-aaa-001"

    def test_prefix_ambiguous(self, companion_module):
        jobs = self.make_jobs()
        with pytest.raises(ValueError, match="ambiguous"):
            companion_module._find_job(jobs, "task")

    def test_no_match(self, companion_module):
        jobs = self.make_jobs()
        with pytest.raises(ValueError, match="No job found"):
            companion_module._find_job(jobs, "nonexistent")

    def test_empty_ref_returns_first(self, companion_module):
        jobs = self.make_jobs()
        result = companion_module._find_job(jobs, "")
        assert result is not None

    def test_empty_ref_empty_list(self, companion_module):
        result = companion_module._find_job([], "")
        assert result is None

    def test_predicate_filter(self, companion_module):
        jobs = self.make_jobs()
        result = companion_module._find_job(jobs, "", lambda j: j["status"] == "running")
        assert result["id"] == "task-bbb-002"

    def test_predicate_no_match(self, companion_module):
        jobs = self.make_jobs()
        with pytest.raises(ValueError, match="No job found"):
            companion_module._find_job(jobs, "task-a", lambda j: j["status"] == "running")


class TestEmit:
    def test_string_non_json(self, companion_module, capsys):
        companion_module.emit("hello world", False)
        captured = capsys.readouterr()
        assert captured.out == "hello world"

    def test_json(self, companion_module, capsys):
        companion_module.emit({"key": "value"}, True)
        captured = capsys.readouterr()
        import json
        assert json.loads(captured.out) == {"key": "value"}

    def test_int_non_json(self, companion_module, capsys):
        companion_module.emit(42, False)
        captured = capsys.readouterr()
        assert captured.out == "42"


class TestRun:
    def test_success(self, companion_module):
        result = companion_module.run(["echo", "hello"], timeout=5)
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_not_found(self, companion_module):
        result = companion_module.run(["nonexistent-cmd-xyz"], timeout=5)
        assert result.returncode == 127

    def test_timeout(self, companion_module):
        result = companion_module.run(["sleep", "10"], timeout=1)
        assert result.returncode == 124
