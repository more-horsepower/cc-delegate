#!/usr/bin/env python3
"""Session lifecycle hook: capture the Claude transcript/session id into the
Claude env file on SessionStart, and reap tracked opencode jobs on SessionEnd.

Run via:  uv run session-lifecycle-hook.py SessionStart|SessionEnd
"""

import json
import os
import sys
from pathlib import Path

SESSION_ID_ENV = "WH_DELEGATE_SESSION_ID"
TRANSCRIPT_PATH_ENV = "WH_DELEGATE_TRANSCRIPT_PATH"
PLUGIN_DATA_ENV = "CLAUDE_PLUGIN_DATA"
COMPANION = Path(__file__).parent / "wh-companion.py"


def read_input() -> dict:
    raw = sys.stdin.read().strip()
    return json.loads(raw) if raw else {}


def shell_escape(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def append_env(name: str, value: str) -> None:
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file or value is None or value == "":
        return
    with open(env_file, "a") as fh:
        fh.write(f"export {name}={shell_escape(value)}\n")


def load_companion():
    """Load wh-companion.py (hyphenated name) as a module via importlib."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("wh_companion", COMPANION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cleanup_jobs(cwd: str, sid: str) -> None:
    if not cwd or not sid:
        return
    try:
        load_companion().reap_session_jobs(cwd, sid)
    except Exception:
        # A lifecycle hook must never crash the host session.
        pass


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    if event == "SessionStart":
        data = read_input()
        append_env(SESSION_ID_ENV, data.get("session_id", ""))
        append_env(TRANSCRIPT_PATH_ENV, data.get("transcript_path", ""))
        append_env(PLUGIN_DATA_ENV, os.environ.get(PLUGIN_DATA_ENV, ""))
    elif event == "SessionEnd":
        data = read_input()
        cleanup_jobs(data.get("cwd") or os.getcwd(), data.get("session_id") or os.environ.get(SESSION_ID_ENV, ""))


if __name__ == "__main__":
    main()
