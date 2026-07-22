---
name: setup
description: Check if Workhorse delegation is ready
---

Verify that the Workhorse delegation environment is ready:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" setup
```

Report the results to the user. If any checks fail, suggest the next steps shown in the output.
