# wh-delegate

Delegate tasks from Claude Code to [opencode](https://opencode.ai) running on [Workhorse](https://aiwork.horse) inference.

## What It Does

When you hand off a task — debugging, implementation, refactoring, investigation — the plugin spawns a **local** opencode process that runs its own agent loop using Workhorse inference (your local node's models via the `wh proxy` localhost tunnel). The opencode agent has full file system access to your workspace, reads and writes files, runs commands, and returns its result to Claude Code.

The subagent calls a zero-dependency Python companion (`wh-companion.py`, run via `uv run`) that spawns `opencode run --format json` and streams the assistant's text back to Claude Code as it's produced. Every task is tracked as a job — so you can check `/wh:status`, fetch `/wh:result`, and `/wh:cancel` background runs — and its opencode session id is recorded so follow-ups can `--resume`.

## Requirements

- **wh CLI** — authenticated (`wh login`)
- **opencode** — on PATH (system install from https://opencode.ai, or wh-managed at `~/.opencode-wh/bin/opencode`)
- **Workhorse proxy** running (`wh proxy on`)
- **opencode providers** configured (`wh opencode setup`)
- **uv** — on PATH (https://docs.astral.sh/uv/). The companion runs via `uv run`, which provisions a Python interpreter automatically if one is not already available.

## Quick Start

```
/plugin marketplace add more-horsepower/cc-delegate
/plugin install wh-delegate@more-horsepower
/reload-plugins
```

Then verify your setup:

```
/wh:setup
```

Delegate a task:

```
/wh:delegate investigate why the integration tests are flaky
```

Or just ask Claude to delegate:

```
Ask Workhorse to refactor the auth module to use the new session manager.
```

Claude will autonomously trigger the `wh-delegate` subagent for substantial tasks.

## Commands

| Command | Description |
|---|---|
| `/wh:delegate <task>` | Delegate a task to opencode on Workhorse inference |
| `/wh:rescue <task>` | Delegate investigation/fix/rescue work to the rescue subagent |
| `/wh:transfer [--source <jsonl>]` | Transfer the current Claude session into a resumable opencode session |
| `/wh:status [job-id]` | Show active and recent opencode jobs (use `--wait` to block) |
| `/wh:result [job-id]` | Show the stored final output for a finished job |
| `/wh:cancel [job-id]` | Cancel an active background opencode job |
| `/wh:setup` | Verify wh, opencode, proxy, and providers are ready |

## How It Works

```
Claude Code (main agent)
    │ delegates via /wh:delegate or wh-delegate subagent triggers autonomously
    ▼
wh-delegate.md / wh-rescue.md (thin forwarding subagent, tools: Bash only)
    │ exactly one Bash call to the companion
    ▼
uv run wh-companion.py task "<task>" [--background] [--resume] [--model <m>]
    │ spawns `opencode run --format json --auto`, parses NDJSON events,
    │ streams assistant text to stdout, records a tracked job (status/result/cancel)
    │ captures the opencode session id for --resume
    ▼
opencode run "<task>" --model workhorse-proxy/default --auto --dir <cwd>
    │ headless, local process, full FS access to workspace
    │ uses Workhorse inference via localhost proxy
    │ agent loop streams JSON events
    ▼
assistant text streams directly to Claude Code

/wh:transfer parses the Claude transcript (captured by the SessionStart hook)
and imports it as an opencode session via `opencode import`, producing a
resumable opencode thread with visible turn history.
```

## Configuration

### Default Model

The plugin uses `workhorse-proxy/default` — which maps to whatever model your Workhorse node has configured as its default. To use a specific model:

```
/wh:delegate --model workhorse-proxy/qwen36-35b-a3b-q4-par1 fix the flaky test
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `WH_DELEGATE_DEFAULT_MODEL` | `workhorse-proxy/default` | Default model for delegated tasks |
| `WH_DELEGATE_SESSION_ID` | _(set by the SessionStart hook)_ | Tags jobs to the current Claude session |
| `WH_DELEGATE_TRANSCRIPT_PATH` | _(set by the SessionStart hook)_ | Claude transcript path for `/wh:transfer` |

### Proxy Port

The Workhorse proxy defaults to port 11969 but can be customized:

```
wh proxy on --port 12345
```

`/wh:setup` detects the actual port from `wh proxy` output — no hardcoding.

## Why This Won't Break

- **No CC internals** — only documented plugin components (agents, commands, hooks, skills, `${CLAUDE_PLUGIN_ROOT}`)
- **No MCP server** — no running process to manage, no protocol version dependency
- **Companion runtime** — a zero-dependency stdlib Python script (`wh-companion.py`) spawned via `uv run`; `opencode run` is the stable contract
- **Output streams naturally** — the companion parses opencode's NDJSON event stream and forwards assistant `text` parts to stdout as they complete
- **Tracked jobs** — every task is recorded so `/wh:status`, `/wh:result`, and `/wh:cancel` work, and `--resume` continues the prior opencode session
- **Workhorse config managed by `wh opencode setup`** — the companion never touches endpoints, API keys, or model lists

## License

MIT
