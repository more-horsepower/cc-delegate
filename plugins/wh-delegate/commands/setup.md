---
name: setup
description: Check if Workhorse delegation (wh, opencode, proxy, providers) is ready
---

Verify that the Workhorse delegation environment is ready:

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" setup --json
```

Present the final setup output to the user. If any checks fail, suggest the next steps shown in the output.

This requires `uv` on PATH (https://docs.astral.sh/uv/). The companion is pure-stdlib Python — `uv run` provisions a Python interpreter automatically if one is not already available.
