---
name: setup
description: Check if Workhorse delegation is ready
---

Verify that the Workhorse delegation environment is ready:

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" setup
```

This requires `uv` on PATH (https://docs.astral.sh/uv/). `uv run` provisions Python 3.13 automatically if it is not already installed.

Report the results to the user. If any checks fail, suggest the next steps shown in the output.
