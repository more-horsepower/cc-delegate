---
name: cancel
description: Cancel an active background opencode job in this repository
argument-hint: '[job-id]'
disable-model-invocation: true
allowed-tools: Bash(uv:*)
---

!`uv run "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" cancel "$ARGUMENTS"`
