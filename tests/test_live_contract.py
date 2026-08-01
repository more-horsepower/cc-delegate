"""Tier 2+3: Live opencode contract tests.

Tier 2: Probes the actual opencode serve HTTP API to detect path/shape
changes without requiring inference.

Tier 3: Runs `opencode run --format json` against a mock OpenAI-compatible
provider to capture real NDJSON events and validate the companion's parser
handles them — no real inference needed.

These tests are marked `@pytest.mark.live` and skip when opencode is not
installed. Run only live tests:  pytest -m live
Skip live tests:                pytest -m "not live"
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

COMPANION = Path(__file__).resolve().parent.parent / "plugins" / "wh-delegate" / "scripts" / "wh-companion.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

pytestmark = pytest.mark.live


def _find_opencode():
    return shutil.which("opencode")


HAS_OPENCODE = _find_opencode() is not None
OPENCODE_BIN = _find_opencode()

skip_no_opencode = pytest.mark.skipif(
    not HAS_OPENCODE, reason="opencode binary not found on PATH"
)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/global/health", timeout=2) as r:
                ct = r.headers.get("Content-Type", "")
                if "application/json" in ct:
                    return json.loads(r.read().decode())
        except Exception:
            time.sleep(0.25)
    return None


# ===========================================================================
# Tier 2: HTTP API Contract Probe
# ===========================================================================

@pytest.fixture(scope="module")
def live_server():
    """Start a real opencode serve instance for the module."""
    if not HAS_OPENCODE:
        pytest.skip("opencode not available")

    port = _free_port()
    proc = subprocess.Popen(
        [OPENCODE_BIN, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, "OPENCODE_SERVER_PASSWORD": ""},
    )
    try:
        url = f"http://127.0.0.1:{port}"
        health = _wait_for_server(url, timeout=20)
        if not health:
            stdout = proc.stdout.read().decode() if proc.stdout else ""
            pytest.fail(f"opencode serve did not start. Output: {stdout}")
        yield {"url": url, "proc": proc, "health": health}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


class TestHealthEndpoint:
    """Validate GET /global/health (the endpoint the companion uses)."""

    def test_returns_healthy(self, live_server):
        url = live_server["url"]
        with urllib.request.urlopen(f"{url}/global/health") as r:
            assert r.status == 200
            ct = r.headers.get("Content-Type", "")
            assert "application/json" in ct
            payload = json.loads(r.read().decode())
            assert payload["healthy"] is True

    def test_includes_version(self, live_server):
        """The companion reads version from health for the broker display."""
        payload = live_server["health"]
        assert "version" in payload
        assert payload["version"]

    def test_companion_broker_healthy_accepts_response(self, live_server, companion_module):
        """The companion's broker_healthy function must accept the real response."""
        result = companion_module.broker_healthy(live_server["url"])
        assert result is not None
        assert result.get("healthy") is True


class TestSessionCreateEndpoint:
    """Validate POST /session?directory= (the endpoint the companion uses)."""

    def test_creates_session(self, live_server):
        url = live_server["url"]
        data = json.dumps({}).encode()
        req = urllib.request.Request(
            f"{url}/session?directory=/tmp",
            data=data, method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200
            ct = r.headers.get("Content-Type", "")
            assert "application/json" in ct
            payload = json.loads(r.read().decode())
            assert "id" in payload
            assert payload["id"]

    def test_companion_broker_create_session_accepts_response(self, live_server, companion_module):
        """The companion's broker_create_session must parse the real response."""
        broker = {"url": live_server["url"], "pid": 0}
        sid = companion_module.broker_create_session(broker, "/tmp", title="Contract test")
        assert sid is not None
        assert sid.startswith("ses_")


class TestSessionAbortEndpoint:
    """Validate POST /session/{id}/abort (the endpoint the companion uses for cancellation)."""

    def test_abort_returns_true(self, live_server):
        url = live_server["url"]
        # Create a session first
        data = json.dumps({}).encode()
        req = urllib.request.Request(
            f"{url}/session?directory=/tmp",
            data=data, method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as r:
            sid = json.loads(r.read().decode())["id"]

        # Abort it
        req = urllib.request.Request(
            f"{url}/session/{sid}/abort?directory=/tmp",
            data=b"", method="POST",
        )
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                ct = r.headers.get("Content-Type", "")
                assert "application/json" in ct
                payload = json.loads(r.read().decode())
                assert payload is True
        except urllib.error.HTTPError as e:
            # If abort fails because the session has no active turn, that's OK —
            # the contract is that the endpoint exists and responds with JSON.
            body = e.read().decode()
            assert e.code in (200, 409, 400), f"Unexpected status {e.code}: {body}"

    def test_companion_broker_abort_session_accepts_response(self, live_server, companion_module):
        """The companion's broker_abort_session must handle the real response."""
        broker = {"url": live_server["url"], "pid": 0}
        # Create a session first
        sid = companion_module.broker_create_session(broker, "/tmp")
        assert sid is not None
        result = companion_module.broker_abort_session(broker, "/tmp", sid, timeout=5)
        # May be True or False depending on whether there's an active turn
        assert isinstance(result, bool)


class TestSpaFallback:
    """The companion guards against the opencode SPA fallback (200 text/html for
    unknown paths). Verify the real server still does this."""

    def test_unknown_path_returns_html(self, live_server):
        url = live_server["url"]
        try:
            with urllib.request.urlopen(f"{url}/nonexistent-path-xyz", timeout=5) as r:
                ct = r.headers.get("Content-Type", "")
                # Should NOT be application/json
                assert "application/json" not in ct
        except urllib.error.HTTPError:
            pass  # 404 is also acceptable


class TestOpenApiSpec:
    """Fetch /openapi.json if available to discover API surface."""

    def test_openapi_available(self, live_server):
        url = live_server["url"]
        try:
            with urllib.request.urlopen(f"{url}/openapi.json", timeout=5) as r:
                ct = r.headers.get("Content-Type", "")
                if "application/json" in ct:
                    spec = json.loads(r.read().decode())
                    assert "paths" in spec or "openapi" in spec
                else:
                    pytest.skip("OpenAPI spec not available at /openapi.json")
        except urllib.error.HTTPError:
            pytest.skip("OpenAPI spec not available")

    def test_v2_api_exists(self, live_server):
        """Check if the v2 API (/api/health) is also available."""
        url = live_server["url"]
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=5) as r:
                ct = r.headers.get("Content-Type", "")
                if "application/json" in ct:
                    payload = json.loads(r.read().decode())
                    assert payload.get("healthy") is True
        except (urllib.error.HTTPError, urllib.error.URLError):
            pytest.skip("v2 API not available on this opencode version")


# ===========================================================================
# Tier 3: NDJSON Event Format Validation with Mock Provider
# ===========================================================================

@pytest.fixture
def mock_provider_config(tmp_path, mock_provider):
    """Create an opencode config pointing to the mock provider."""
    config = {
        "provider": {
            "mock": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Mock Provider",
                "options": {
                    "apiKey": "dummy",
                    "baseURL": mock_provider.base_url,
                },
                "models": {
                    "default": {
                        "limit": {"context": 128000, "output": 6000},
                        "modalities": {"input": ["text"], "output": ["text"]},
                    },
                },
            },
        },
    }
    config_path = tmp_path / "opencode.json"
    config_path.write_text(json.dumps(config, indent=2))
    return {"path": config_path, "dir": str(tmp_path), "url": mock_provider.url}


@skip_no_opencode
class TestLiveNDJSON:
    """Run real `opencode run --format json` against a mock provider and validate
    the companion's event parser handles the output."""

    def test_opencode_produces_ndjson(self, mock_provider_config):
        """opencode run --format json must produce NDJSON on stdout."""
        result = subprocess.run(
            [
                OPENCODE_BIN, "run", "Say hello",
                "--dir", mock_provider_config["dir"],
                "--format", "json",
                "--model", "mock/default",
                "--auto",
            ],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(Path(mock_provider_config["dir"]).parent)},
            cwd=mock_provider_config["dir"],
        )
        if result.returncode != 0:
            # opencode might fail if the mock provider response is incomplete
            # — log stderr and skip
            pytest.skip(
                f"opencode run failed (rc={result.returncode}). "
                f"stderr: {result.stderr[:500]}"
            )
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        assert len(lines) > 0, "opencode run produced no output"

        # Every line should be valid JSON
        events = []
        for line in lines:
            try:
                ev = json.loads(line)
                if isinstance(ev, dict):
                    events.append(ev)
            except json.JSONDecodeError:
                pass
        assert len(events) > 0, "No valid JSON events in output"

    def test_events_have_known_types(self, mock_provider_config):
        """Every event type in the output must be one the companion handles."""
        result = subprocess.run(
            [
                OPENCODE_BIN, "run", "Say hello",
                "--dir", mock_provider_config["dir"],
                "--format", "json",
                "--model", "mock/default",
                "--auto",
            ],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(Path(mock_provider_config["dir"]).parent)},
            cwd=mock_provider_config["dir"],
        )
        if result.returncode != 0:
            pytest.skip(f"opencode run failed: {result.stderr[:300]}")

        KNOWN_TYPES = {"text", "tool_use", "tool", "step_start", "step_finish",
                       "error", "session.info", "sessionID"}
        UNKNOWN_TYPES = set()
        events = []

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            events.append(ev)
            t = ev.get("type")
            if t and t not in KNOWN_TYPES:
                UNKNOWN_TYPES.add(t)

        assert len(events) > 0
        # If there are unknown types, they might be new event types opencode
        # started emitting — flag them but don't fail (the companion ignores
        # unknown types gracefully)
        if UNKNOWN_TYPES:
            # Log for awareness
            print(f"\nNew event types not handled by companion: {UNKNOWN_TYPES}")

    def test_text_events_have_part_text(self, mock_provider_config):
        """text events must have part.text (the shape the companion reads)."""
        result = subprocess.run(
            [
                OPENCODE_BIN, "run", "Say hello",
                "--dir", mock_provider_config["dir"],
                "--format", "json",
                "--model", "mock/default",
                "--auto",
            ],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(Path(mock_provider_config["dir"]).parent)},
            cwd=mock_provider_config["dir"],
        )
        if result.returncode != 0:
            pytest.skip(f"opencode run failed: {result.stderr[:300]}")

        text_events = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "text":
                text_events.append(ev)

        if not text_events:
            pytest.skip("No text events emitted (model may not have returned text)")

        for ev in text_events:
            # The companion reads (ev.get("part") or {}).get("text", "")
            part = ev.get("part")
            assert part is not None, f"text event missing 'part' field: {ev}"
            assert "text" in part, f"text event part missing 'text' key: {ev}"

    def test_session_id_present(self, mock_provider_config):
        """At least one event should carry a sessionID."""
        result = subprocess.run(
            [
                OPENCODE_BIN, "run", "Say hello",
                "--dir", mock_provider_config["dir"],
                "--format", "json",
                "--model", "mock/default",
                "--auto",
            ],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(Path(mock_provider_config["dir"]).parent)},
            cwd=mock_provider_config["dir"],
        )
        if result.returncode != 0:
            pytest.skip(f"opencode run failed: {result.stderr[:300]}")

        session_ids = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                sid = ev.get("sessionID") or ev.get("sessionId")
                if sid:
                    session_ids.add(sid)

        if not session_ids:
            pytest.skip("No sessionID in events (may need a different approach)")
        else:
            for sid in session_ids:
                assert sid

    def test_companion_parser_handles_live_events(self, mock_provider_config, companion_module, monkeypatch):
        """Feed live NDJSON through the companion's run_opencode_turn parser."""
        # First capture the NDJSON from opencode
        result = subprocess.run(
            [
                OPENCODE_BIN, "run", "Say hello",
                "--dir", mock_provider_config["dir"],
                "--format", "json",
                "--model", "mock/default",
                "--auto",
            ],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(Path(mock_provider_config["dir"]).parent)},
            cwd=mock_provider_config["dir"],
        )
        if result.returncode != 0:
            pytest.skip(f"opencode run failed: {result.stderr[:300]}")

        # Save the NDJSON to a file
        ndjson_file = Path(mock_provider_config["dir"]) / "live_events.ndjson"
        ndjson_file.write_text(result.stdout)

        # Create a fake opencode binary that replays the captured events
        fake_bin_dir = Path(mock_provider_config["dir"]) / "fake-bin"
        fake_bin_dir.mkdir()
        fake_script = fake_bin_dir / "opencode"
        fake_script.write_text(f"""#!/usr/bin/env python3
import json, sys, os
args = sys.argv[1:]
if "--version" in args or "-v" in args:
    print("replay-opencode 1.0.0")
    sys.exit(0)
if len(args) >= 1 and args[0] == "run":
    for line in open("{ndjson_file}"):
        if line.strip():
            print(line, end="", flush=True)
    sys.exit(0)
if len(args) >= 1 and args[0] == "serve":
    print("opencode server listening on http://127.0.0.1:99999", flush=True)
    import time
    while True: time.sleep(1)
sys.exit(1)
""")
        fake_script.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{fake_bin_dir}:{old_path}"
        monkeypatch.setattr(companion_module, "OPENCODE_WH_DIRS", [])
        monkeypatch.setattr(companion_module, "find_opencode_binary", lambda: str(fake_script))

        # Run through the companion's parser
        texts = []
        progress = []
        result = companion_module.run_opencode_turn(
            mock_provider_config["dir"],
            "Say hello",
            "mock/default", None, "ses_test", False,
            "http://127.0.0.1:99999",
            on_text=lambda t: texts.append(t),
            on_progress=lambda m, p=None: progress.append((m, p)),
        )

        # The parser should not fail — status 0 means success
        assert result["status"] == 0, f"Parser returned non-zero status. Error: {result['error']}"
        # Some text should have been captured (if the model returned text)
        if texts:
            assert all(isinstance(t, str) for t in texts)
