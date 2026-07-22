---
name: wh-runtime
description: Internal helper contract for calling the wh-companion runtime from Claude Code. Used only by the wh-delegate subagent.
user-invocable: false
---

# Workhorse Delegate Runtime

Use this skill only inside the `wh-delegate` subagent.

## Primary Helper

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" task "<prompt>"
```

## Execution Rules

- The delegate subagent is a **forwarder**, not an orchestrator. Its only job is to invoke `task` once and return that stdout unchanged.
- Do not call `setup`, `status`, `result`, or `cancel` from the delegate subagent.
- Use `task` for every delegation request.
- Leave model unset by default (uses `workhorse-proxy/default` — the user's workhorse node default). Only add `--model workhorse-proxy/<name>` when the user explicitly asks for a specific model.
- Preserve the user's task text as-is apart from stripping routing flags.
- Return the stdout of the `task` command exactly as-is.
- If the Bash call fails or opencode cannot be invoked, return nothing.

## Command Selection

- Use exactly one `task` invocation per delegation.
- If the request includes `--model`, pass it through to `task`.

## Flag Reference

| Flag | Description |
|---|---|
| `--model <provider/model>` | Override the default model (e.g. `workhorse-proxy/qwen36-35b-a3b-q4-par1`) |

## What Happens Under the Hood

When `task` is called:

1. The companion script resolves the opencode binary (prefers system, falls back to wh-managed)
2. Verifies the workhorse proxy is running
3. Spawns `opencode run "<prompt>" --model <model> --dangerously-skip-permissions --dir <cwd> --format json`
4. opencode runs locally with full file system access, using Workhorse inference via the localhost proxy
5. The agent loop completes and the companion extracts the assistant's response
6. The response text is printed to stdout and returned to Claude Code

The opencode agent has full read/write access to the workspace. It can read files, write code, run commands — anything a normal coding agent can do.
