"""Shared test fixtures for cc-delegate.

Loads wh-companion.py (hyphenated name) via importlib, same technique the
session-lifecycle-hook uses. Provides fixtures for isolated state dirs,
fake opencode binaries, mock broker servers, and mock OpenAI-compatible
providers.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "wh-delegate" / "scripts"
COMPANION_PATH = SCRIPTS_DIR / "wh-companion.py"
HOOK_PATH = SCRIPTS_DIR / "session-lifecycle-hook.py"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def companion_module():
    """Load wh-companion.py as a Python module."""
    return _load_module(COMPANION_PATH, "wh_companion_test")


@pytest.fixture(scope="session")
def hook_module():
    """Load session-lifecycle-hook.py as a Python module."""
    return _load_module(HOOK_PATH, "session_lifecycle_hook_test")


# ---------------------------------------------------------------------------
# Workspace + state isolation
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_workspace(tmp_path):
    """A temporary git repo for testing workspace_root() and state dirs."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def plugin_data_dir(tmp_path):
    """Set CLAUDE_PLUGIN_DATA so state goes to a temp dir."""
    data_dir = tmp_path / "plugin-data"
    data_dir.mkdir()
    old = os.environ.get("CLAUDE_PLUGIN_DATA")
    os.environ["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    yield data_dir
    if old is not None:
        os.environ["CLAUDE_PLUGIN_DATA"] = old
    else:
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)


@pytest.fixture
def session_env(plugin_data_dir):
    """Set a test session ID."""
    old = os.environ.get("WH_DELEGATE_SESSION_ID")
    os.environ["WH_DELEGATE_SESSION_ID"] = "test-session-123"
    yield "test-session-123"
    if old is not None:
        os.environ["WH_DELEGATE_SESSION_ID"] = old
    else:
        os.environ.pop("WH_DELEGATE_SESSION_ID", None)


@pytest.fixture
def workspace(tmp_workspace, plugin_data_dir, session_env):
    """Full isolation: git repo + state dir + session ID. Returns the cwd."""
    return str(tmp_workspace)


# ---------------------------------------------------------------------------
# Port allocation helper
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Mock broker HTTP server (simulates opencode serve)
# ---------------------------------------------------------------------------

class MockBrokerHandler(BaseHTTPRequestHandler):
    """Simulates the opencode serve HTTP API for Tier 1 tests."""

    def log_message(self, *args):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, html="<html></html>"):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/global/health":
            self._send_json(200, {"healthy": True, "version": "mock-1.0.0"})
        elif self.path.startswith("/api/health"):
            self._send_json(200, {"healthy": True})
        else:
            self._send_html(200)

    def do_POST(self):
        if self.path.startswith("/session") or self.path.startswith("/api/session"):
            if "/abort" in self.path or "/interrupt" in self.path:
                self._send_json(200, True)
            else:
                self._send_json(200, {
                    "id": "ses_mock_001",
                    "slug": "test-session",
                    "projectID": "global",
                    "directory": "/tmp",
                    "cost": 0,
                    "tokens": {"input": 0, "output": 0, "reasoning": 0,
                               "cache": {"read": 0, "write": 0}},
                    "title": "Mock session",
                    "version": "mock-1.0.0",
                    "time": {"created": int(time.time() * 1000),
                             "updated": int(time.time() * 1000)},
                })
        else:
            self._send_html(200)


class MockBroker:
    """Context manager that starts and stops a mock opencode serve broker."""

    def __init__(self, handler_cls=MockBrokerHandler):
        self._port = _free_port()
        self._server = HTTPServer(("127.0.0.1", self._port), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self._port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture
def mock_broker():
    """A running mock opencode serve broker on localhost."""
    with MockBroker() as b:
        yield b


# ---------------------------------------------------------------------------
# Fake opencode binary (for Tier 1 subprocess tests)
# ---------------------------------------------------------------------------

FAKE_OPENCODE_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, sys, os, time, threading, socket
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    def free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def main():
        args = sys.argv[1:]

        if "--version" in args or "-v" in args:
            print("fake-opencode 99.99.99")
            return

        if len(args) >= 1 and args[0] == "serve":
            port = free_port()
            if "--port" in args:
                idx = args.index("--port")
                if idx + 1 < len(args) and args[idx + 1] != "0":
                    port = int(args[idx + 1])

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *a): pass
                def _json(self, code, payload):
                    body = json.dumps(payload).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def do_GET(self):
                    if self.path == "/global/health":
                        self._json(200, {"healthy": True, "version": "fake-1.0.0"})
                    elif self.path == "/api/health":
                        self._json(200, {"healthy": True})
                    else:
                        body = b"<html></html>"
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                def do_POST(self):
                    if "/abort" in self.path or "/interrupt" in self.path:
                        self._json(200, True)
                    elif "/session" in self.path:
                        self._json(200, {"id": "ses_fake_001", "projectID": "global",
                                         "directory": os.getcwd(),
                                         "cost": 0, "tokens": {"input":0,"output":0,"reasoning":0,
                                         "cache":{"read":0,"write":0}},
                                         "title": "Fake session",
                                         "version": "fake-1.0.0",
                                         "time": {"created": int(time.time()*1000),
                                                  "updated": int(time.time()*1000)}})
                    else:
                        body = b"<html></html>"
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

            srv = HTTPServer(("127.0.0.1", port), Handler)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            print(f"opencode server listening on http://127.0.0.1:{port}", flush=True)
            sys.stdout.flush()
            while True:
                time.sleep(1)

        if len(args) >= 1 and args[0] == "run":
            ndjson_file = os.environ.get("FAKE_OPENCODE_NDJSON", "")
            if ndjson_file and Path(ndjson_file).exists():
                for line in Path(ndjson_file).read_text().splitlines():
                    if line.strip():
                        print(line, flush=True)
            elif os.environ.get("FAKE_OPENCODE_FAIL"):
                print(json.dumps({"type": "error", "error": {"name": "Error",
                    "data": {"message": "fake failure"}}}), flush=True)
                sys.exit(1)
            else:
                sid = os.environ.get("FAKE_OPENCODE_SESSION_ID", "ses_fake_run_001")
                print(json.dumps({"type": "session.info", "sessionID": sid}), flush=True)
                print(json.dumps({"type": "step_start"}), flush=True)
                print(json.dumps({"type": "text", "part": {"text": "Hello from fake opencode"}}), flush=True)
                print(json.dumps({"type": "step_finish"}), flush=True)
            return

        if len(args) >= 1 and args[0] == "import":
            print("Imported session: ses_fake_imported_001", flush=True)
            return

        if len(args) >= 1 and args[0] == "models":
            print("workhorse-proxy/default")
            return

        print("fake-opencode: unknown command", file=sys.stderr)
        sys.exit(1)

    main()
""")


@pytest.fixture
def fake_opencode(tmp_path):
    """Create a fake `opencode` binary on PATH. Returns its path."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "opencode"
    script.write_text(FAKE_OPENCODE_SCRIPT)
    script.chmod(0o755)

    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}:{old_path}"

    # Also set OPENCODE_WH_DIRS to include our fake binary
    old_wh = os.environ.get("OPENCODE_WH_DIRS")

    yield str(script)

    os.environ["PATH"] = old_path
    if old_wh is not None:
        os.environ["OPENCODE_WH_DIRS"] = old_wh
    else:
        os.environ.pop("OPENCODE_WH_DIRS", None)


# ---------------------------------------------------------------------------
# NDJSON fixture files
# ---------------------------------------------------------------------------

@pytest.fixture
def ndjson_dir():
    return FIXTURES_DIR / "ndjson"


# ---------------------------------------------------------------------------
# Mock OpenAI-compatible provider (for Tier 3 live tests)
# ---------------------------------------------------------------------------

class MockProviderHandler(BaseHTTPRequestHandler):
    """Mock OpenAI-compatible provider that returns canned chat completions."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            self._stream_response()
        elif self.path == "/v1/models":
            body = json.dumps({"data": [{"id": "mock-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path == "/v1/models":
            body = json.dumps({"data": [{"id": "mock-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def _stream_response(self):
        """Return a minimal SSE streaming chat completion."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        chunks = [
            {"choices": [{"delta": {"content": "Hello"}, "index": 0}]},
            {"choices": [{"delta": {"content": " from mock"}, "index": 0}]},
            {"choices": [{"delta": {"content": " provider"}, "index": 0,
                          "finish_reason": "stop"}]},
        ]
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class MockProvider:
    """Context manager for the mock OpenAI-compatible provider."""

    def __init__(self):
        self._port = _free_port()
        self._server = HTTPServer(("127.0.0.1", self._port), MockProviderHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self._port}"

    @property
    def base_url(self):
        return f"{self.url}/v1"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture
def mock_provider():
    """A running mock OpenAI-compatible provider on localhost."""
    with MockProvider() as p:
        yield p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def make_companion_proc(tmp_path):
    """Factory to run wh-companion.py as a subprocess with controlled env."""
    def _run(*args, env=None, cwd=None, timeout=30):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        proc = subprocess.run(
            [sys.executable, str(COMPANION_PATH), *args],
            capture_output=True, text=True, timeout=timeout,
            env=full_env, cwd=cwd or str(tmp_path),
        )
        return proc
    return _run


def wait_for_port(port, timeout=5):
    """Wait until a port is accepting connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


def http_get_json(url, timeout=5):
    """Simple GET that returns parsed JSON or None."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if "application/json" not in (resp.headers.get("Content-Type") or ""):
                return None
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else None
    except Exception:
        return None
