#!/usr/bin/env python3
"""Workhorse delegate setup script.

Verify environment readiness for opencode delegation on Workhorse inference.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

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

    Searches system opencode on PATH and the wh-managed install at
    ~/.opencode-wh/bin/, then picks the candidate with the highest version.
    The wh-managed version can lag behind and may have bugs, so the newest
    available binary wins regardless of source.
    """
    candidates: list[tuple[str, str]] = []

    # 1. System opencode on PATH
    system_bin = shutil.which("opencode")
    if system_bin:
        ver = get_opencode_version(system_bin)
        if ver:
            candidates.append((system_bin, ver))

    # 2. wh-managed opencode (pinned version, installed by wh spectate/attach)
    for candidate in [
        Path.home() / ".opencode-wh" / "bin" / "opencode",
        Path.home() / ".opencode-wh" / "opencode",
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
        url_match = re.search(r"base_url:\s*(\S+)", output)
        if url_match:
            return True, url_match.group(1)
        return True, "running"
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


# --- Main ---


def print_usage() -> None:
    print(
        "Usage:\n"
        "  wh-companion.py setup\n"
    )


def main() -> int:
    if len(sys.argv) < 2:
        print_usage()
        return 1

    subcommand = sys.argv[1]

    if subcommand == "setup":
        return cmd_setup()
    elif subcommand in ("help", "--help", "-h"):
        print_usage()
        return 0
    else:
        err(f"Unknown subcommand: {subcommand}")
        err("Only 'setup' is supported. Delegation is handled directly by the subagent via 'opencode run'.")
        print_usage()
        return 1


if __name__ == "__main__":
    sys.exit(main())
