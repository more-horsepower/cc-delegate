# wh-delegate

Delegate tasks from Claude Code to [opencode](https://opencode.ai) running on [Workhorse](https://aiwork.horse) inference.

## What It Does

When you hand off a task — debugging, implementation, refactoring, investigation — the plugin spawns a **local** opencode process that runs its own agent loop using Workhorse inference (your local node's models via the `wh proxy` localhost tunnel). The opencode agent has full file system access to your workspace, reads and writes files, runs commands, and returns its result to Claude Code.

This is the companion-script pattern popularized by [openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc): a thin forwarding subagent + a Python companion script that shells out to a local agent CLI.

## Requirements

- **wh CLI** — authenticated (`wh login`)
- **opencode** — either wh-managed (`~/.opencode-wh/bin/opencode`, installed automatically by `wh spectate`/`wh attach`) or system `opencode`
- **Workhorse proxy** running (`wh proxy on`)
- **opencode providers** configured (`wh opencode setup`)

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
| `/wh:setup` | Verify wh, opencode, proxy, and providers are ready |

## How It Works

```
Claude Code (main agent)
    │ delegates via /wh:delegate or wh-delegate subagent triggers autonomously
    ▼
wh-delegate.md (thin forwarding subagent, tools: Bash only)
    │ exactly one Bash call to companion script
    ▼
wh-companion.py (companion script)
    │ spawns: opencode run "task" --model workhorse-proxy/default --auto --dir <cwd> --format json
    ▼
opencode (headless, local process, full FS access to workspace)
    │ uses workhorse inference via localhost proxy (port 11969)
    │ agent loop runs to completion — reads/writes files, runs commands
    ▼
returns result text → companion → Claude Code
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
| `WH_DELEGATE_OPENCODE_BIN` | auto-detected | Override the opencode binary path |
| `WH_DELEGATE_DEFAULT_MODEL` | `workhorse-proxy/default` | Default model for delegated tasks |
| `WH_DELEGATE_TIMEOUT` | `600` | Timeout in seconds for opencode runs |

## Why This Won't Break

- **No CC internals** — only documented plugin components (agents, commands, skills, `${CLAUDE_PLUGIN_ROOT}`)
- **No MCP server** — no running process to manage, no protocol version dependency
- **Companion-script pattern** — validated by OpenAI's own codex-plugin-cc
- **opencode CLI is the contract** — `opencode run` is stable; the companion script is the only thing that touches it
- **Workhorse config managed by `wh opencode setup`** — the companion script never touches endpoints, API keys, or model lists

## License

MIT
