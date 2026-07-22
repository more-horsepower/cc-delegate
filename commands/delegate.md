---
name: delegate
description: Delegate a task to opencode running on Workhorse inference
---

Delegate the following task to Workhorse (opencode running on Workhorse inference). The task will run locally with full file system access to the current workspace.

Task:
$ARGUMENTS

Use the `wh-delegate` subagent to forward this task. The subagent will make a single Bash call to the companion script, which spawns `opencode run` with the task prompt.

If the user specified `--model <provider/model>`, pass it through. Otherwise use the workhorse default.

The delegated agent will work autonomously — reading files, writing code, running commands as needed — and return its result. Share the result with the user when it completes.
