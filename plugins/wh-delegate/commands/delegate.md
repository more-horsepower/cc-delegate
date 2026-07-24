---
name: delegate
description: Delegate a task to opencode running on Workhorse inference
---

Delegate the following task to Workhorse (opencode running on Workhorse inference). The task will run locally with full file system access to the current workspace.

Task:
$ARGUMENTS

Use the `wh-delegate` subagent to forward this task. The subagent will make a single Bash call to `opencode run` directly.

If the user explicitly asked for a specific model (e.g. `--model workhorse-proxy/<name>` or "use the qwen 35b model"), pass `--model` through. Otherwise leave it unset so the workhorse default is used. The `wh-delegate` subagent applies this rule authoritatively — see its instructions for the exact forwarding command.

The delegated agent will work autonomously — reading files, writing code, running commands as needed — and return its result. Share the result with the user when it completes.
