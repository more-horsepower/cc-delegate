---
name: delegate
description: Delegate a task to opencode running on Workhorse inference
argument-hint: "[--background|--wait] [--resume|--fresh] [--model <model>] [--variant <variant>] [task to delegate]"
allowed-tools: Bash(uv:*), Bash(Bash:*), AskUserQuestion, Agent
---

Delegate the following task to Workhorse (opencode running on Workhorse inference). The task runs locally with full file system access to the current workspace.

Task:
$ARGUMENTS

Execution mode:

- If the request includes `--background`, run the task in the background via the companion and return the queued job id.
- If the request includes `--wait`, run the task in the foreground (default).
- `--background` and `--wait` are execution flags for Claude Code. Do not forward them to the task text.
- `--model` and `--variant` are runtime-selection flags. Preserve them for the forwarded call, but do not treat them as part of the task text.
- If the request includes `--resume`, continue the most recent opencode task session for this repository (add `--resume` to the companion call).
- If the request includes `--fresh`, start a new opencode session (do not add `--resume`).
- Otherwise, before starting, check for a resumable task from this Claude session by running:

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" task-resume-candidate --json
```

- If that helper reports `available: true`, use `AskUserQuestion` exactly once to ask whether to continue the current opencode session or start a new one.
- The two choices must be:
  - `Continue current opencode session`
  - `Start a new opencode session`
- If the user is clearly giving a follow-up instruction such as "continue", "keep going", "resume", "apply the top fix", or "dig deeper", put `Continue current opencode session (Recommended)` first.
- Otherwise put `Start a new opencode session (Recommended)` first.
- If the user chooses continue, add `--resume` before routing to the companion.
- If the user chooses a new session, add `--fresh` before routing to the companion.
- If the helper reports `available: false`, do not ask. Route normally.

Operating rules:

- Run exactly one `Bash` call to invoke:
  ```bash
  uv run "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" task [flags] "<task text>"
  ```
- Return the companion stdout verbatim to the user.
- Do not paraphrase, summarize, rewrite, or add commentary before or after it.
- Do not inspect files, monitor progress, poll `/wh:status`, fetch `/wh:result`, call `/wh:cancel`, or do follow-up work of its own.
- Leave `--model` unset unless the user explicitly asks for a specific model (e.g. `--model workhorse-proxy/qwen36-35b-a3b-q4-par1`).
- Leave `--variant` unset unless the user explicitly asks for a specific reasoning effort.
- If the helper reports that opencode is missing, stop and tell the user to run `/wh:setup`.
- If the user did not supply a task, ask what Workhorse should do.
