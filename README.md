# wh-delegate

Delegate tasks from Claude Code to [opencode](https://opencode.ai) running on [Workhorse](https://aiwork.horse) inference.

## What It Does

When you hand off a task — debugging, implementation, refactoring, investigation — the plugin spawns a **local** opencode process that runs its own agent loop using Workhorse inference (your local node's models via the `wh proxy` localhost tunnel). The opencode agent has full file system access to your workspace, reads and writes files, runs commands, and returns its result to Claude Code.

The subagent calls `opencode run` directly via a single Bash call — no Python intermediary, so output streams live to Claude Code as it's produced.

## Requirements

- **wh CLI** — authenticated (`wh login`)
- **opencode** — on PATH (system install from https://opencode.ai, or wh-managed at `~/.opencode-wh/bin/opencode`)
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
    │ exactly one Bash call to opencode run
    ▼
opencode run "<task>" --model workhorse-proxy/default --auto --dir <cwd>
    │ headless, local process, full FS access to workspace
    │ uses Workhorse inference via localhost proxy
    │ agent loop streams output as it runs
    ▼
output streams directly to Claude Code
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

### Proxy Port

The Workhorse proxy defaults to port 11969 but can be customized:

```
wh proxy on --port 12345
```

`/wh:setup` detects the actual port from `wh proxy` output — no hardcoding.

## Why This Won't Break

- **No CC internals** — only documented plugin components (agents, commands, skills, `${CLAUDE_PLUGIN_ROOT}`)
- **No MCP server** — no running process to manage, no protocol version dependency
- **Direct CLI invocation** — the subagent calls `opencode run` directly; `opencode run` is the stable contract
- **Output streams naturally** — no buffering intermediary; CC's Bash tool receives opencode output as it's produced
- **Workhorse config managed by `wh opencode setup`** — the subagent never touches endpoints, API keys, or model lists

## License

MIT
