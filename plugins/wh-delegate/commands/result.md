---
name: result
description: Show the stored final output for a finished opencode job in this repository
argument-hint: '[job-id]'
disable-model-invocation: true
allowed-tools: Bash(uv:*)
---

!`uv run "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" result "$ARGUMENTS"`

Present the full command output to the user. Do not summarize or condense it. Preserve all details including:
- Job ID and status
- The complete result payload (opencode's final message)
- The opencode session ID and resume command
- Any error messages
- Follow-up commands such as `/wh:status <id>` and `/wh:rescue --resume`
