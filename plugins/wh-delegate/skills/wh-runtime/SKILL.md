---
name: wh-runtime
description: Internal helper contract for the companion runtime used by the wh-delegate and wh-rescue subagents.
user-invocable: false
---

# Workhorse Delegate Runtime

Use this skill only inside the `wh-delegate` and `wh-rescue` subagents.

## Primary Helper

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" task "<prompt>"
```

The companion spawns `opencode run --format json --auto` and streams the assistant's text output back on stdout.

## Execution Rules

- The delegate/rescue subagent is a **forwarder**, not an orchestrator. Its only job is to invoke the companion once and return the output unchanged.
- Do not call `setup`, `status`, `result`, or `cancel` from the delegate/rescue subagent.
- Use the `task` subcommand for every delegation request.
- Leave the model unset by default (the companion falls back to `$WH_DELEGATE_DEFAULT_MODEL`, then `workhorse-proxy/default`). Only add `--model workhorse-proxy/<name>` when the user explicitly asks for a specific model.
- Leave `--variant` unset unless the user explicitly asks for a specific reasoning effort.
- Preserve the user's task text as-is apart from stripping routing flags.
- Return the stdout exactly as-is — the companion streams opencode's human-readable output.
- If the Bash call fails or opencode cannot be invoked, return a single line so the user knows delegation did not run: `wh-delegate: opencode invocation failed (<brief reason>)` (or `wh-rescue: ...` from the rescue subagent). Do not swallow the failure silently.

## Command Selection

- Use exactly one `task` invocation per delegation.
- For follow-up work in the same session, add `--resume` to continue the most recent opencode task session.
- For a fresh start, add `--fresh` (the default when neither `--resume` nor `--fresh` is present).
- For long-running or open-ended work, add `--background` to run the task detached; the companion returns a job id you can check with `/wh:status` and `/wh:result`.

## Flag Reference

| Flag | Description |
|---|---|
| `--model <provider/model>` | Override the default model (e.g. `workhorse-proxy/qwen36-35b-a3b-q4-par1`) |
| `--variant <variant>` | Provider-specific reasoning effort (e.g. `high`, `max`, `minimal`) |
| `--background` | Run the task detached and return a job id |
| `--resume` / `--resume-last` | Continue the most recent opencode task session for this repo |
| `--fresh` | Start a new opencode session (do not resume) |
| `--prompt-file <path>` | Read the prompt from a file instead of the command line |

## What Happens Under the Hood

When the companion runs `task`:

1. It resolves the workspace root (git toplevel, or the cwd if not in a repo).
2. It spawns `opencode run "<prompt>" --model <model> --format json --auto --dir <cwd>` (plus `--session <id>` when resuming), in its own process group so it can be cancelled.
3. opencode runs locally with full file system access, using Workhorse inference via the localhost proxy.
4. The companion parses the NDJSON event stream: `step_start`, `tool_use`, `text`, `step_finish`, `error`. It forwards assistant `text` parts to stdout as they complete and records progress in a job log.
5. The session id (opencode `sessionID`) is captured and stored on the job so it can be resumed later with `--resume`.

The opencode agent has full read/write access to the workspace. It can read files, write code, run commands — anything a normal coding agent can do.
