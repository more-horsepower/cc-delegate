#!/usr/bin/env python3
"""Session lifecycle hook: capture the Claude transcript/session id into the
Claude env file on SessionStart, and on SessionEnd cancel this session's
tracked opencode jobs (via the broker abort API, so turns stop cleanly) and
stop the workspace broker once the last Claude session lease goes away.

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


def on_session_start(cwd: str, sid: str) -> None:
    if not cwd or not sid:
        return
    try:
        load_companion().lease_start(cwd, sid)
    except Exception:
        # A lifecycle hook must never crash the host session.
        pass


def on_session_end(cwd: str, sid: str) -> None:
    if not cwd or not sid:
        return
    try:
        comp = load_companion()
        # Abort this session's opencode turns through the broker (clean stop,
        # jobs recorded as cancelled), then drop this session's broker lease
        # and stop the broker when no Claude session is using it anymore.
        comp.reap_session_jobs(cwd, sid)
        if comp.lease_end(cwd, sid) == 0:
            comp.stop_broker(cwd)
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
        on_session_start(data.get("cwd") or os.getcwd(), data.get("session_id", ""))
    elif event == "SessionEnd":
        data = read_input()
        on_session_end(data.get("cwd") or os.getcwd(), data.get("session_id") or os.environ.get(SESSION_ID_ENV, ""))


if __name__ == "__main__":
    main()
