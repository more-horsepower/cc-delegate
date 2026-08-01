"""Tier 1: Transcript transfer tests.

Tests build_opencode_export_from_transcript (Claude JSONL → opencode import
format) and resolve_transcript_path validation.
"""

import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "transcripts"


class TestBuildExportSimple:
    def test_simple_session(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "simple_session.jsonl"), str(tmp_workspace)
        )
        assert "info" in result
        assert "messages" in result
        assert len(result["messages"]) == 2  # 1 user + 1 assistant

    def test_user_message_structure(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "simple_session.jsonl"), str(tmp_workspace)
        )
        user_msg = result["messages"][0]
        assert user_msg["info"]["role"] == "user"
        assert user_msg["info"]["sessionID"] == result["info"]["id"]
        assert user_msg["info"]["agent"] == "build"
        assert any(p["type"] == "text" for p in user_msg["parts"])
        assert user_msg["parts"][0]["text"] == "Fix the flaky integration tests"

    def test_assistant_message_structure(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "simple_session.jsonl"), str(tmp_workspace)
        )
        asst_msg = result["messages"][1]
        assert asst_msg["info"]["role"] == "assistant"
        assert asst_msg["info"]["modelID"] == "claude-sonnet-4-20250514"
        assert asst_msg["info"]["providerID"] == "anthropic"
        assert asst_msg["info"]["mode"] == "primary"
        assert asst_msg["info"]["agent"] == "build"
        assert asst_msg["info"]["path"]["cwd"] == str(tmp_workspace)
        assert asst_msg["info"]["finish"] == "end_turn"
        assert asst_msg["info"]["tokens"]["input"] == 100
        assert asst_msg["info"]["tokens"]["output"] == 50

    def test_message_ids_sequential(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "simple_session.jsonl"), str(tmp_workspace)
        )
        ids = [m["info"]["id"] for m in result["messages"]]
        # IDs should contain _claude_ prefix and be unique
        assert len(ids) == len(set(ids))
        assert all("_claude_" in mid for mid in ids)

    def test_parent_linkage(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "session_with_tools.jsonl"), str(tmp_workspace)
        )
        # First assistant message links to the preceding user message
        first_asst = next(m for m in result["messages"] if m["info"]["role"] == "assistant")
        first_user = next(m for m in result["messages"] if m["info"]["role"] == "user")
        assert first_asst["info"]["parentID"] == first_user["info"]["id"]
        # Subsequent assistant messages link to previous message
        msgs = result["messages"]
        for i in range(1, len(msgs)):
            prev_id = msgs[i - 1]["info"]["id"]
            if msgs[i]["info"]["role"] == "assistant":
                assert msgs[i]["info"]["parentID"] == prev_id


class TestBuildExportWithTools:
    def test_tool_use_and_result_pairing(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "session_with_tools.jsonl"), str(tmp_workspace)
        )
        # Find the first assistant message with a tool_use block
        asst_with_tool = None
        for msg in result["messages"]:
            if msg["info"]["role"] == "assistant":
                if any(p.get("type") == "tool" for p in msg["parts"]):
                    asst_with_tool = msg
                    break
        assert asst_with_tool is not None

        # The tool part should be "completed" after its tool_result was processed
        tool_part = next(p for p in asst_with_tool["parts"] if p.get("type") == "tool")
        assert tool_part["state"]["status"] == "completed"
        assert tool_part["tool"] == "Read"
        assert tool_part["callID"] == "toolu_001"

    def test_tool_result_completes_tool(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "session_with_tools.jsonl"), str(tmp_workspace)
        )
        # The tool_use with id "toolu_001" should be completed after its tool_result
        asst_msg = result["messages"][1]  # First assistant with tool_use
        tool_part = next(p for p in asst_msg["parts"] if p.get("type") == "tool" and p["callID"] == "toolu_001")
        assert tool_part["state"]["status"] == "completed"
        assert "output" in tool_part["state"]
        assert "def test_foo" in tool_part["state"]["output"]

    def test_second_tool_pair(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "session_with_tools.jsonl"), str(tmp_workspace)
        )
        # messages: [0]user, [1]assistant(toolu_001), [2]assistant(toolu_002), [3]assistant(text)
        asst_msg = result["messages"][2]
        tool_part = next(p for p in asst_msg["parts"] if p.get("type") == "tool" and p["callID"] == "toolu_002")
        assert tool_part["state"]["status"] == "completed"
        assert "File written successfully" in tool_part["state"]["output"]

    def test_message_count(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "session_with_tools.jsonl"), str(tmp_workspace)
        )
        # 2 user text messages + 2 tool_result-only user messages (skipped) + 3 assistant messages
        # Wait: the tool_result user messages are tool_only and should be skipped
        # So: user(text) + assistant + [user(tool_result) SKIPPED] + assistant + [user(tool_result) SKIPPED] + assistant
        # = 1 + 1 + 1 + 1 = 4 messages
        assert len(result["messages"]) == 4


class TestBuildExportEdgeCases:
    def test_skips_summary_and_system(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "with_summary_system.jsonl"), str(tmp_workspace)
        )
        # Only the user and assistant messages should be included
        assert len(result["messages"]) == 2
        assert result["messages"][0]["info"]["role"] == "user"
        assert result["messages"][1]["info"]["role"] == "assistant"

    def test_skips_tool_only_user(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "tool_only_user.jsonl"), str(tmp_workspace)
        )
        assert result["messages"] == []

    def test_skips_malformed_lines(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "malformed.jsonl"), str(tmp_workspace)
        )
        # The second valid line should produce one message
        assert len(result["messages"]) == 1

    def test_string_content_converted_to_text_blocks(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "simple_session.jsonl"), str(tmp_workspace)
        )
        user_msg = result["messages"][0]
        # Content was a string, should be converted to a text part
        assert len(user_msg["parts"]) == 1
        assert user_msg["parts"][0]["type"] == "text"

    def test_empty_file(self, companion_module, tmp_workspace):
        empty = tmp_workspace / "empty.jsonl"
        empty.write_text("")
        result = companion_module.build_opencode_export_from_transcript(
            str(empty), str(tmp_workspace)
        )
        assert result["messages"] == []
        assert "info" in result

    def test_blanks_lines_skipped(self, companion_module, tmp_workspace):
        f = tmp_workspace / "blanks.jsonl"
        f.write_text('\n\n  \n{"type":"user","message":{"role":"user","content":"hi"},"timestamp":"2026-01-01T10:00:00Z"}\n\n')
        result = companion_module.build_opencode_export_from_transcript(
            str(f), str(tmp_workspace)
        )
        assert len(result["messages"]) == 1


class TestBuildExportSessionInfo:
    def test_session_id_format(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "simple_session.jsonl"), str(tmp_workspace)
        )
        assert result["info"]["id"].startswith("ses_claude_")

    def test_title_from_first_user(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "simple_session.jsonl"), str(tmp_workspace)
        )
        assert "flaky" in result["info"]["title"].lower()

    def test_custom_title(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "simple_session.jsonl"), str(tmp_workspace), title="Custom Title"
        )
        assert result["info"]["title"] == "Custom Title"

    def test_directory_set(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "simple_session.jsonl"), str(tmp_workspace)
        )
        assert result["info"]["directory"] == str(tmp_workspace)

    def test_permissions_denied(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "simple_session.jsonl"), str(tmp_workspace)
        )
        perms = result["info"]["permission"]
        assert len(perms) == 3
        actions = {p["action"] for p in perms}
        assert actions == {"deny"}

    def test_slug_from_first_user(self, companion_module, tmp_workspace):
        result = companion_module.build_opencode_export_from_transcript(
            str(FIXTURES / "simple_session.jsonl"), str(tmp_workspace)
        )
        slug = result["info"]["slug"]
        assert "fix" in slug.lower() or "flaky" in slug.lower() or "claude-transfer" in slug

    def test_empty_transcript_slug_fallback(self, companion_module, tmp_workspace):
        empty = tmp_workspace / "empty.jsonl"
        empty.write_text("")
        result = companion_module.build_opencode_export_from_transcript(
            str(empty), str(tmp_workspace)
        )
        assert result["info"]["slug"].startswith("claude-transfer")


class TestResolveTranscriptPath:
    def test_valid_path(self, companion_module, tmp_path, monkeypatch):
        # Create a fake projects dir
        projects = tmp_path / ".claude" / "projects" / "test-project"
        projects.mkdir(parents=True)
        transcript = projects / "session.jsonl"
        transcript.write_text("{}")
        monkeypatch.setattr(companion_module, "PROJECTS_DIR", projects.parent.resolve())

        result = companion_module.resolve_transcript_path(str(tmp_path), str(transcript))
        assert result == str(transcript.resolve())

    def test_missing_source_and_env(self, companion_module, tmp_path, monkeypatch):
        monkeypatch.delenv("WH_DELEGATE_TRANSCRIPT_PATH", raising=False)
        with pytest.raises(SystemExit):
            companion_module.resolve_transcript_path(str(tmp_path), None)

    def test_wrong_extension(self, companion_module, tmp_path, monkeypatch):
        f = tmp_path / "session.txt"
        f.write_text("test")
        with pytest.raises(SystemExit):
            companion_module.resolve_transcript_path(str(tmp_path), str(f))

    def test_nonexistent_file(self, companion_module, tmp_path, monkeypatch):
        with pytest.raises(SystemExit):
            companion_module.resolve_transcript_path(str(tmp_path), str(tmp_path / "nonexistent.jsonl"))

    def test_outside_projects_dir(self, companion_module, tmp_path, monkeypatch):
        f = tmp_path / "session.jsonl"
        f.write_text("{}")
        monkeypatch.setattr(companion_module, "PROJECTS_DIR", (tmp_path / "other").resolve())
        with pytest.raises(SystemExit):
            companion_module.resolve_transcript_path(str(tmp_path), str(f))

    def test_relative_path_resolved(self, companion_module, tmp_path, monkeypatch):
        projects = tmp_path / ".claude" / "projects" / "proj"
        projects.mkdir(parents=True)
        transcript = projects / "session.jsonl"
        transcript.write_text("{}")
        monkeypatch.setattr(companion_module, "PROJECTS_DIR", projects.parent.resolve())

        result = companion_module.resolve_transcript_path(
            str(projects), "session.jsonl"
        )
        assert result == str(transcript.resolve())
