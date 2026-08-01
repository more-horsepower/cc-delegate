"""Tier 1: NDJSON event parsing tests.

Tests run_opencode_turn() by using a fake opencode binary that emits canned
NDJSON event streams. Validates that the companion correctly parses all event
types (text, tool_use, step_start, step_finish, error, abort) and handles
malformed input.
"""

import json
import os
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ndjson"


@pytest.fixture
def patched_binary(companion_module, fake_opencode, monkeypatch):
    """Monkeypatch find_opencode_binary to return our fake binary,
    bypassing version sorting that might pick a real opencode."""
    monkeypatch.setattr(companion_module, "OPENCODE_WH_DIRS", [])
    monkeypatch.setattr(companion_module, "find_opencode_binary", lambda: fake_opencode)
    return fake_opencode


def run_turn(companion_module, cwd, ndjson_file=None, fail=False, session_id="ses_test",
             broker_url="http://127.0.0.1:99999", prompt="test prompt"):
    """Helper: run a turn with a specific NDJSON fixture or failure mode."""
    env = {}
    if ndjson_file:
        env["FAKE_OPENCODE_NDJSON"] = str(ndjson_file)
    if fail:
        env["FAKE_OPENCODE_FAIL"] = "1"
    if session_id:
        env["FAKE_OPENCODE_SESSION_ID"] = session_id

    old_env = {}
    for k, v in env.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v

    texts = []
    progress = []
    start_pid = [None]

    try:
        result = companion_module.run_opencode_turn(
            cwd,
            prompt,
            model="mock/default",
            variant=None,
            session_id=session_id,
            is_resume=False,
            broker_url=broker_url,
            on_text=lambda txt: texts.append(txt),
            on_progress=lambda msg, phase=None: progress.append((msg, phase)),
            on_start=lambda pid: start_pid.__setitem__(0, pid),
        )
    finally:
        for k, old in old_env.items():
            if old is not None:
                os.environ[k] = old
            else:
                os.environ.pop(k, None)

    return result, texts, progress, start_pid[0]


class TestSimpleText:
    def test_basic_text(self, companion_module, patched_binary, tmp_workspace):
        result, texts, progress, pid = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "simple_text.ndjson"
        )
        assert result["status"] == 0
        assert result["aborted"] is False
        assert "I'll help you with that." in result["raw"]
        assert "Here is the solution." in result["raw"]
        assert texts == ["I'll help you with that.", "Here is the solution."]
        assert pid is not None and pid > 0

    def test_step_events(self, companion_module, patched_binary, tmp_workspace):
        result, texts, progress, pid = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "simple_text.ndjson"
        )
        # Should have step_start and step_finish progress events
        msgs = [p[0] for p in progress]
        assert "Step started" in msgs
        assert "Step finished" in msgs

    def test_session_id_extracted(self, companion_module, patched_binary, tmp_workspace):
        result, _, _, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "simple_text.ndjson",
            session_id="ses_test_001"
        )
        assert result["sessionId"] == "ses_test_001"


class TestToolUse:
    def test_tool_events(self, companion_module, patched_binary, tmp_workspace):
        result, _, progress, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "text_with_tools.ndjson"
        )
        msgs = [p[0] for p in progress]
        assert any("Running tool: read" in m for m in msgs)
        assert any("Tool completed: read" in m for m in msgs)
        assert any("Running tool: write" in m for m in msgs)
        assert any("Tool completed: write" in m for m in msgs)

    def test_tool_phases(self, companion_module, patched_binary, tmp_workspace):
        result, _, progress, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "text_with_tools.ndjson"
        )
        phases = [p[1] for p in progress if p[1]]
        assert "investigating" in phases

    def test_tool_with_title(self, companion_module, patched_binary, tmp_workspace):
        result, _, progress, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "text_with_tools.ndjson"
        )
        msgs = [p[0] for p in progress]
        assert any("Reading main.py" in m for m in msgs)
        assert any("Writing main.py" in m for m in msgs)

    def test_multi_step_tools(self, companion_module, patched_binary, tmp_workspace):
        result, _, progress, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "multi_step_tools.ndjson"
        )
        msgs = [p[0] for p in progress]
        assert any("Tool failed: bash" in m for m in msgs)
        assert "All tests pass now." in result["raw"]


class TestAborted:
    def test_aborted_sets_flag(self, companion_module, patched_binary, tmp_workspace):
        result, _, progress, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "aborted.ndjson"
        )
        assert result["aborted"] is True
        assert "Turn aborted." in [p[0] for p in progress]

    def test_aborted_not_failure(self, companion_module, patched_binary, tmp_workspace):
        result, _, _, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "aborted.ndjson"
        )
        # Aborted turns should not set error
        assert result["error"] == ""
        assert result["status"] == 0


class TestErrorEvents:
    def test_error_sets_failure(self, companion_module, patched_binary, tmp_workspace):
        result, _, progress, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "error.ndjson"
        )
        assert result["status"] == 1
        assert "Model connection failed" in result["error"]

    def test_error_progress(self, companion_module, patched_binary, tmp_workspace):
        result, _, progress, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "error.ndjson"
        )
        msgs = [p[0] for p in progress]
        assert any("opencode error: Model connection failed" in m for m in msgs)
        assert any("failed" in (p[1] or "") for p in progress)


class TestMultiStep:
    def test_multiple_steps(self, companion_module, patched_binary, tmp_workspace):
        result, _, progress, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "multi_step.ndjson"
        )
        step_starts = [p for p in progress if p[0] == "Step started"]
        step_finishes = [p for p in progress if p[0] == "Step finished"]
        assert len(step_starts) == 3
        assert len(step_finishes) == 3
        assert "First step." in result["raw"]
        assert "Second step." in result["raw"]
        assert "Third step." in result["raw"]


class TestEmptyTurn:
    def test_no_text(self, companion_module, patched_binary, tmp_workspace):
        result, texts, _, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "empty_turn.ndjson"
        )
        assert result["status"] == 0
        assert result["raw"] == ""
        assert texts == []


class TestNoSessionId:
    def test_session_id_preserved(self, companion_module, patched_binary, tmp_workspace):
        """When the NDJSON stream doesn't include a sessionID, the passed
        session_id should be preserved."""
        result, _, _, _ = run_turn(
            companion_module, str(tmp_workspace),
            ndjson_file=FIXTURES / "no_session_id.ndjson",
            session_id="ses_passed_in"
        )
        assert result["sessionId"] == "ses_passed_in"


class TestMalformedNDJSON:
    def test_skips_bad_lines(self, companion_module, patched_binary, tmp_workspace):
        result, _, _, _ = run_turn(
            companion_module, str(tmp_workspace), ndjson_file=FIXTURES / "malformed.ndjson"
        )
        # Should still get the valid text event
        assert "After bad line" in result["raw"]
        assert result["status"] == 0


class TestFakeFailure:
    def test_fake_fail(self, companion_module, patched_binary, tmp_workspace):
        result, _, _, _ = run_turn(
            companion_module, str(tmp_workspace), fail=True
        )
        assert result["status"] == 1
        assert "fake failure" in result["error"]


class TestBinaryNotFound:
    def test_no_binary(self, companion_module, monkeypatch, tmp_workspace):
        monkeypatch.setattr(companion_module, "find_opencode_binary", lambda: None)
        result = companion_module.run_opencode_turn(
            str(tmp_workspace), "test", "mock/default", None, "ses_123",
            False, "http://127.0.0.1:1",
            on_text=lambda t: None, on_progress=lambda m, p=None: None,
        )
        assert result["status"] == 1
        assert "not found" in result["error"].lower()

    def test_empty_prompt(self, companion_module, patched_binary, tmp_workspace):
        result = companion_module.run_opencode_turn(
            str(tmp_workspace), "", "mock/default", None, "ses_123",
            False, "http://127.0.0.1:1",
            on_text=lambda t: None, on_progress=lambda m, p=None: None,
        )
        # Empty prompt with is_resume=False should fail
        assert result["status"] == 1
