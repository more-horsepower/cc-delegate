---
name: wh-runtime
description: Internal helper contract for the direct opencode delegation used by the wh-delegate subagent.
user-invocable: false
---

# Workhorse Delegate Runtime

Use this skill only inside the `wh-delegate` subagent.

## Primary Helper

```bash
opencode run '<prompt>' --model "${WH_DELEGATE_DEFAULT_MODEL:-workhorse-proxy/default}" --auto --dir "$PWD"
```

Single-quote the prompt so shell metacharacters in the user's task are not interpreted by bash. If the prompt contains a single quote, escape it as `'\''`.

## Execution Rules

- The delegate subagent is a **forwarder**, not an orchestrator. Its only job is to invoke `opencode run` once and return the output unchanged.
- Do not call `setup`, `status`, `result`, or `cancel` from the delegate subagent.
- Use the direct `opencode run` command for every delegation request.
- Leave model unset by default (uses `workhorse-proxy/default`). Only add `--model workhorse-proxy/<name>` when the user explicitly asks for a specific model.
- Preserve the user's task text as-is apart from stripping routing flags.
- Return the stdout exactly as-is — opencode streams human-readable output with `--auto` and default format.
- If the Bash call fails or opencode cannot be invoked, return a single line so the user knows delegation did not run: `wh-delegate: opencode invocation failed (<brief reason>)`. Do not swallow the failure silently.

## Command Selection

- Use exactly one `opencode run` invocation per delegation.
- If the request includes `--model`, pass it through.

## Flag Reference

| Flag | Description |
|---|---|
| `--model <provider/model>` | Override the default model (e.g. `workhorse-proxy/qwen36-35b-a3b-q4-par1`) |

## What Happens Under the Hood

When `opencode run` is called:

1. The subagent executes `opencode run "<prompt>" --model workhorse-proxy/default --auto --dir "$PWD"`
2. opencode runs locally with full file system access, using Workhorse inference via the localhost proxy
3. The agent loop completes — opencode reads files, writes code, runs commands as needed
4. The output streams through to stdout in human-readable format (default)
5. The output is returned directly to Claude Code

The opencode agent has full read/write access to the workspace. It can read files, write code, run commands — anything a normal coding agent can do.
