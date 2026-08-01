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

The companion runs the turn as `opencode run --attach <broker> --session <id> --format json --auto` against a persistent per-workspace `opencode serve` broker and streams the assistant's text output back on stdout.

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

1. It resolves the workspace root (git toplevel, or the cwd if not in a repo) and ensures a persistent `opencode serve` broker is running for that workspace (started on demand; lifecycle-managed by the SessionStart/SessionEnd hooks via per-session leases).
2. It creates the opencode session up front through the broker API (`POST /session?directory=...`), so every job carries its opencode session id from the start — unless `--resume` continues an existing session.
3. It spawns `opencode run "<prompt>" --attach <broker-url> --session <id> --model <model> --format json --auto --dir <cwd>`.
4. opencode runs locally with full file system access, using Workhorse inference via the localhost proxy.
5. The companion parses the NDJSON event stream: `step_start`, `tool_use`, `text`, `step_finish`, `error`. It forwards assistant `text` parts to stdout as they complete and records progress in a job log. A `MessageAbortedError` event means the turn was cleanly aborted (cancelled), not failed.

Cancellation (`/wh:cancel`, or SessionEnd cleanup) goes through the broker's `POST /session/{id}/abort` API: opencode stops the turn itself, flushes its session database, marks the session idle, and leaves it resumable — the job is recorded as cancelled, never as a crash.

The opencode agent has full read/write access to the workspace. It can read files, write code, run commands — anything a normal coding agent can do.
