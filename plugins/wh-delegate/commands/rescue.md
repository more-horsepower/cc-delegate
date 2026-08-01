---
name: rescue
description: Delegate investigation, an explicit fix request, or follow-up rescue work to the Workhorse rescue subagent
argument-hint: "[--background|--wait] [--resume|--fresh] [--model <model>] [--variant <variant>] [what opencode should investigate, solve, or continue]"
allowed-tools: Bash(uv:*), Bash(Bash:*), AskUserQuestion, Agent
---

Invoke the `wh:wh-rescue` subagent via the `Agent` tool (`subagent_type: "wh:wh-rescue"`), forwarding the raw user request as the prompt.
`wh:wh-rescue` is a subagent, not a skill — do not call `Skill(wh:wh-rescue)` (no such skill) or `Skill(wh:rescue)` (that re-enters this command and hangs the session). The command runs inline so the `Agent` tool stays in scope; forked general-purpose subagents do not expose it.
The final user-visible response must be the rescue subagent's output verbatim.

Raw user request:
$ARGUMENTS

Execution mode:

- If the request includes `--background`, run the `wh:wh-rescue` subagent in the background.
- If the request includes `--wait`, run the `wh:wh-rescue` subagent in the foreground.
- If neither flag is present, default to foreground.
- `--background` and `--wait` are execution flags for Claude Code. Do not forward them to the subagent, and do not treat them as part of the natural-language task text.
- `--model` and `--variant` are runtime-selection flags. Preserve them for the forwarded `task` call, but do not treat them as part of the natural-language task text.
- If the request includes `--resume`, do not ask whether to continue. The user already chose.
- If the request includes `--fresh`, do not ask whether to continue. The user already chose.
- Otherwise, before starting opencode, check for a resumable rescue thread from this Claude session by running:

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" task-resume-candidate --json
```

- If that helper reports `available: true`, use `AskUserQuestion` exactly once to ask whether to continue the current opencode session or start a new one.
- The two choices must be:
  - `Continue current opencode session`
  - `Start a new opencode session`
- If the user is clearly giving a follow-up instruction such as "continue", "keep going", "resume", "apply the top fix", or "dig deeper", put `Continue current opencode session (Recommended)` first.
- Otherwise put `Start a new opencode session (Recommended)` first.
- If the user chooses continue, add `--resume` before routing to the subagent.
- If the user chooses a new session, add `--fresh` before routing to the subagent.
- If the helper reports `available: false`, do not ask. Route normally.

Operating rules:

- The subagent is a thin forwarder only. It should use one `Bash` call to invoke `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" task ...` and return that command's stdout as-is.
- Return the companion stdout verbatim to the user.
- Do not paraphrase, summarize, rewrite, or add commentary before or after it.
- Do not ask the subagent to inspect files, monitor progress, poll `/wh:status`, fetch `/wh:result`, call `/wh:cancel`, summarize output, or do follow-up work of its own.
- Leave `--variant` unset unless the user explicitly asks for a specific reasoning effort.
- Leave the model unset unless the user explicitly asks for one.
- Leave `--resume` and `--fresh` in the forwarded request. The subagent handles that routing when it builds the `task` command.
- If the helper reports that opencode is missing, stop and tell the user to run `/wh:setup`.
- If the user did not supply a request, ask what opencode should investigate or fix.
