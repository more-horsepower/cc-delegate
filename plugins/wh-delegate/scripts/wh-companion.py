#!/usr/bin/env python3
"""Workhorse delegate companion script.

Bridge between Claude Code and opencode running on Workhorse inference.
Shells out to `opencode run` (headless mode) which runs a full agent loop
locally using Workhorse inference via the localhost proxy.

Subcommands:
  task    Delegate a task to opencode on Workhorse inference
  setup   Verify wh, opencode, proxy, and providers are ready

Usage:
  python wh-companion.py task "fix the flaky test"
  python wh-companion.py task --model workhorse-proxy/qwen36-35b-a3b-q4-par1 "refactor auth"
  python wh-companion.py setup
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# --- Constants ---

DEFAULT_MODEL = os.environ.get("WH_DELEGATE_DEFAULT_MODEL", "workhorse-proxy/default")
DEFAULT_TIMEOUT = int(os.environ.get("WH_DELEGATE_TIMEOUT", "600"))
WH_MANAGED_OPENCODE_DIR = Path.home() / ".opencode-wh" / "bin"
WH_PROXY_PORT = 11969

# opencode run --format json emits newline-delimited JSON objects.
# Each object has a "type" field. We want the final assistant message.
# Types observed: session.updated, message.updated, message.part, etc.
# The final assistant message text is in the last message with role "assistant".

# --- Helpers ---


def err(msg: str) -> None:
    print(msg, file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    err(msg)
    sys.exit(code)


def run_command(
    cmd: list[str],
    *,
    timeout: int = 30,
    capture: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command and return the completed process."""
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=check,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, returncode=124, stdout="", stderr="timed out"
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            cmd, returncode=127, stdout="", stderr=f"command not found: {cmd[0]}"
        )


def _parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a version string like '1.18.4' into (1, 18, 4). Returns (0,) on failure."""
    nums = re.findall(r"\d+", version_str)
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def find_opencode_binary() -> tuple[str | None, str | None]:
    """Find the opencode binary to use.

    Returns (path, version) or (None, None) if not found.

    Preference order:
    1. Explicit override via WH_DELEGATE_OPENCODE_BIN
    2. System opencode on PATH (typically the latest, avoids wh-managed bugs)
    3. wh-managed opencode at ~/.opencode-wh/bin/ (pinned by wh CLI)

    The wh-managed version can lag behind and may have bugs (e.g. 1.15.5 has a
    SQLite session_message.seq NOT NULL bug). System opencode is preferred when
    available and newer.
    """
    candidates: list[tuple[str, str]] = []

    # 1. Explicit override via env var
    env_bin = os.environ.get("WH_DELEGATE_OPENCODE_BIN")
    if env_bin and shutil.which(env_bin):
        ver = get_opencode_version(env_bin)
        if ver:
            candidates.append((env_bin, ver))

    # 2. System opencode on PATH (preferred — typically latest)
    system_bin = shutil.which("opencode")
    if system_bin:
        ver = get_opencode_version(system_bin)
        if ver:
            candidates.append((system_bin, ver))

    # 3. wh-managed opencode (pinned version, installed by wh spectate/attach)
    for candidate in [
        WH_MANAGED_OPENCODE_DIR / "opencode",
        WH_MANAGED_OPENCODE_DIR / "opencode.cmd",
    ]:
        if candidate.exists() and os.access(candidate, os.X_OK):
            ver = get_opencode_version(str(candidate))
            if ver:
                candidates.append((str(candidate), ver))

    if not candidates:
        return None, None

    # Pick the candidate with the highest version
    best = max(candidates, key=lambda c: _parse_version(c[1]))
    return best


def get_opencode_version(binary_path: str) -> str | None:
    """Get opencode version string."""
    result = run_command([binary_path, "--version"], timeout=10)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def find_wh_cli() -> str | None:
    """Find the wh CLI binary."""
    return shutil.which("wh")


def check_wh_auth(wh_bin: str) -> tuple[bool, str]:
    """Check if wh is authenticated. Returns (authenticated, username)."""
    result = run_command([wh_bin, "whoami"], timeout=15)
    if result.returncode == 0:
        username = result.stdout.strip()
        return True, username
    return False, ""


def check_proxy_running(wh_bin: str) -> tuple[bool, str]:
    """Check if the workhorse proxy is running. Returns (running, info)."""
    result = run_command([wh_bin, "proxy"], timeout=15)
    output = result.stdout + result.stderr
    if result.returncode == 0 and "Proxy running" in output:
        # Extract base_url if present
        url_match = re.search(r"base_url:\s*(\S+)", output)
        url = url_match.group(1) if url_match else f"http://127.0.0.1:{WH_PROXY_PORT}/v1"
        return True, url
    return False, ""


def check_opencode_providers(opencode_bin: str) -> list[str]:
    """Check which workhorse models are available in opencode. Returns model list."""
    result = run_command(
        [opencode_bin, "models", "workhorse-proxy"], timeout=15
    )
    if result.returncode != 0:
        return []
    models = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("workhorse-proxy/"):
            models.append(line)
    return models


# --- Setup subcommand ---


def cmd_setup() -> int:
    """Verify environment readiness."""
    print("Workhorse Delegate — Setup Check")
    print("=" * 50)
    print()

    all_ready = True

    # 1. wh CLI
    print("1. wh CLI")
    wh_bin = find_wh_cli()
    if wh_bin:
        authed, username = check_wh_auth(wh_bin)
        if authed:
            print(f"   OK  found: {wh_bin}")
            print(f"   OK  authenticated as: {username}")
        else:
            print(f"   OK  found: {wh_bin}")
            print(f"   FAIL  not authenticated. Run: wh login")
            all_ready = False
    else:
        print("   FAIL  wh CLI not found on PATH")
        print("         Install: curl -fsSL https://app.aiwork.horse/install/wh | bash")
        all_ready = False
    print()

    # 2. opencode
    print("2. opencode")
    oc_bin, oc_ver = find_opencode_binary()
    if oc_bin:
        print(f"   OK  found: {oc_bin}")
        print(f"   OK  version: {oc_ver}")
    else:
        print("   FAIL  opencode not found")
        print("         wh-managed: run 'wh attach <run>' or 'wh spectate' to install")
        print("         system: install from https://opencode.ai")
        all_ready = False
    print()

    # 3. Workhorse proxy
    print("3. Workhorse proxy")
    if wh_bin:
        proxy_running, proxy_url = check_proxy_running(wh_bin)
        if proxy_running:
            print(f"   OK  running at {proxy_url}")
        else:
            print("   FAIL  proxy not running")
            print("         Start with: wh proxy on")
            all_ready = False
    else:
        print("   SKIP  cannot check (wh CLI not found)")
        all_ready = False
    print()

    # 4. opencode providers
    print("4. opencode providers")
    if oc_bin:
        models = check_opencode_providers(oc_bin)
        if models:
            print(f"   OK  {len(models)} model(s) available:")
            for m in models[:5]:
                print(f"        - {m}")
            if len(models) > 5:
                print(f"        ... and {len(models) - 5} more")
        else:
            print("   FAIL  no workhorse-proxy models found in opencode")
            print("         Run: wh opencode setup")
            all_ready = False
    else:
        print("   SKIP  cannot check (opencode not found)")
        all_ready = False
    print()

    # Summary
    print("=" * 50)
    if all_ready:
        print("All checks passed. Ready to delegate!")
        print()
        print("Try: /wh:delegate fix the flaky integration test")
    else:
        print("Some checks failed. Fix the issues above, then re-run /wh:setup.")

    return 0 if all_ready else 1


# --- Task subcommand ---


def parse_task_args(argv: list[str]) -> tuple[str | None, str]:
    """Parse task subcommand arguments.

    Returns (model_override, prompt_text).
    """
    model = None
    prompt_parts: list[str] = []

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--model", "-m"):
            if i + 1 < len(argv):
                model = argv[i + 1]
                i += 2
            else:
                die(f"Error: {arg} requires a value")
        elif arg.startswith("--model="):
            model = arg[len("--model=") :]
            i += 1
        elif arg.startswith("-"):
            die(f"Error: unknown flag: {arg}")
        else:
            prompt_parts.append(arg)
            i += 1

    prompt = " ".join(prompt_parts).strip()
    return model, prompt


def extract_result_from_json_output(raw_output: str) -> str:
    """Extract the final assistant message from opencode run --format json output.

    opencode emits newline-delimited JSON events. Each event has a "type" field.
    The relevant types are:

      {"type": "text", "part": {"type": "text", "text": "...", ...}}
      {"type": "step_start", "part": {"type": "step-start", ...}}
      {"type": "step_finish", "part": {"type": "step-finish", ...}}
      {"type": "tool_start", "part": {"type": "tool-start", ...}}
      {"type": "tool_finish", "part": {"type": "tool-finish", ...}}
      {"type": "error", "error": {"name": "...", "data": {"message": "..."}}}

    Assistant text arrives in "text" events. We collect all text parts and
    return them concatenated (the full assistant response).

    Falls back to raw output if no text events are found.
    """
    text_parts: list[str] = []

    for line in raw_output.strip().splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")

        # Text content from the assistant
        if event_type == "text":
            part = event.get("part", {})
            if isinstance(part, dict):
                text = part.get("text", "")
                if text:
                    text_parts.append(text)

        # Error event
        if event_type == "error":
            error = event.get("error", {})
            error_data = error.get("data", {}) if isinstance(error, dict) else {}
            error_msg = error_data.get("message", "") if isinstance(error_data, dict) else ""
            if error_msg:
                text_parts.append(f"[Error from opencode: {error_msg}]")

    if text_parts:
        return "\n".join(text_parts)

    # Fallback: return raw output (non-JSON or unrecognized format)
    return raw_output.strip()


def cmd_task(argv: list[str]) -> int:
    """Delegate a task to opencode on Workhorse inference."""
    model_override, prompt = parse_task_args(argv)

    if not prompt:
        die(
            "Error: no task provided.\n"
            "Usage: wh-companion.py task [--model <provider/model>] <prompt>"
        )

    # Resolve opencode binary
    oc_bin, oc_ver = find_opencode_binary()
    if not oc_bin:
        die(
            "Error: opencode not found.\n"
            "  wh-managed: run 'wh attach <run>' or 'wh spectate' to install\n"
            "  system: install from https://opencode.ai"
        )

    # Resolve model
    model = model_override or DEFAULT_MODEL

    # Resolve working directory
    cwd = os.getcwd()

    # Quick proxy check (non-fatal — user might be using workhorse-api)
    wh_bin = find_wh_cli()
    if wh_bin:
        proxy_running, _ = check_proxy_running(wh_bin)
        if not proxy_running and model.startswith("workhorse-proxy"):
            err("Warning: workhorse proxy is not running. Start it with: wh proxy on")
            err("  (delegation may fail if the proxy is required for this model)")
            err()

    # Build opencode command.
    #
    # opencode run flags (verified against opencode 1.15+):
    #   --model <provider/model>     model to use
    #   --dangerously-skip-permissions  auto-approve all tool calls (headless mode)
    #   --dir <path>                 working directory
    #   --format json                newline-delimited JSON event output
    #
    # Note: --auto is a global opencode flag (for TUI mode), NOT accepted by
    # the `run` subcommand. The `run` equivalent is --dangerously-skip-permissions.
    cmd = [
        oc_bin,
        "run",
        prompt,
        "--model",
        model,
        "--dangerously-skip-permissions",
        "--dir",
        cwd,
        "--format",
        "json",
    ]

    # Run opencode
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        die(
            f"Error: opencode timed out after {DEFAULT_TIMEOUT}s.\n"
            f"  Set WH_DELEGATE_TIMEOUT to increase the limit."
        )
        return 124  # unreachable, die exits
    except KeyboardInterrupt:
        die("\nCancelled.")
        return 130

    if result.returncode != 0:
        # opencode failed — print stderr and exit
        if result.stderr:
            err(result.stderr)
        die(f"Error: opencode exited with code {result.returncode}")

    # Parse output
    output = extract_result_from_json_output(result.stdout)

    # Print result to stdout (this is what Claude Code receives)
    print(output)
    return 0


# --- Main ---


def print_usage() -> None:
    print(
        "Usage:\n"
        "  wh-companion.py task [--model <provider/model>] <prompt>\n"
        "  wh-companion.py setup\n"
    )


def main() -> int:
    if len(sys.argv) < 2:
        print_usage()
        return 1

    subcommand = sys.argv[1]
    argv = sys.argv[2:]

    if subcommand == "task":
        return cmd_task(argv)
    elif subcommand == "setup":
        return cmd_setup()
    elif subcommand in ("help", "--help", "-h"):
        print_usage()
        return 0
    else:
        err(f"Unknown subcommand: {subcommand}")
        print_usage()
        return 1


if __name__ == "__main__":
    sys.exit(main())
