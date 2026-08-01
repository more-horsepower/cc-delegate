---
name: transfer
description: Transfer the current Claude Code session into a resumable opencode session
argument-hint: "[--source <claude-jsonl>]"
disable-model-invocation: true
allowed-tools: Bash(uv:*)
---

!`uv run "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" transfer "$ARGUMENTS"`

Present the command output to the user exactly as returned. Preserve the opencode session ID and the resume command.

The transcript path is captured automatically by the `SessionStart` hook into `$WH_DELEGATE_TRANSCRIPT_PATH`. If the hook did not run (for example, when transferring a session other than the current one), pass `--source <path-to-claude-jsonl>` — the file must live under `~/.claude/projects/`.
