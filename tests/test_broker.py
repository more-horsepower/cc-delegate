"""Tier 1: Broker HTTP API mock tests.

Tests the companion's HTTP client for the opencode serve broker API using
a local mock HTTP server. Validates the SPA-fallback content-type guard.
"""

import json
import os
import time
from pathlib import Path

import pytest


class TestHttpJson:
    def test_get_json(self, companion_module, mock_broker):
        ok, payload = companion_module._http_json("GET", f"{mock_broker.url}/global/health")
        assert ok is True
        assert payload["healthy"] is True
        assert payload["version"] == "mock-1.0.0"

    def test_post_json(self, companion_module, mock_broker):
        ok, payload = companion_module._http_json(
            "POST", f"{mock_broker.url}/session?directory=/tmp", payload={}
        )
        assert ok is True
        assert payload["id"] == "ses_mock_001"

    def test_spa_fallback_rejected(self, companion_module, mock_broker):
        """The opencode SPA fallback answers unknown paths with 200 text/html.
        The companion must reject these via content-type check."""
        ok, payload = companion_module._http_json("GET", f"{mock_broker.url}/unknown-path")
        assert ok is False
        assert payload is None

    def test_connection_refused(self, companion_module):
        ok, payload = companion_module._http_json("GET", "http://127.0.0.1:1/global/health", timeout=1)
        assert ok is False
        assert payload is None

    def test_timeout(self, companion_module):
        # Use a non-routable address to trigger timeout
        ok, payload = companion_module._http_json("GET", "http://10.0.0.1/global/health", timeout=1)
        assert ok is False


class TestBrokerHealthy:
    def test_healthy(self, companion_module, mock_broker):
        result = companion_module.broker_healthy(mock_broker.url)
        assert result is not None
        assert result["healthy"] is True

    def test_unreachable(self, companion_module):
        result = companion_module.broker_healthy("http://127.0.0.1:1", timeout=1)
        assert result is None

    def test_none_url(self, companion_module):
        result = companion_module.broker_healthy(None)
        assert result is None

    def test_empty_url(self, companion_module):
        result = companion_module.broker_healthy("")
        assert result is None


class TestBrokerCreateSession:
    def test_creates_session(self, companion_module, mock_broker):
        broker = {"url": mock_broker.url, "pid": 12345}
        sid = companion_module.broker_create_session(broker, "/tmp", title="Test session")
        assert sid == "ses_mock_001"

    def test_with_title(self, companion_module, mock_broker):
        broker = {"url": mock_broker.url, "pid": 12345}
        sid = companion_module.broker_create_session(broker, "/workspace", title="Custom title")
        assert sid is not None

    def test_no_title(self, companion_module, mock_broker):
        broker = {"url": mock_broker.url, "pid": 12345}
        sid = companion_module.broker_create_session(broker, "/tmp")
        assert sid is not None

    def test_url_encoded_directory(self, companion_module, mock_broker):
        broker = {"url": mock_broker.url, "pid": 12345}
        sid = companion_module.broker_create_session(broker, "/path with spaces")
        assert sid is not None

    def test_unreachable_returns_none(self, companion_module):
        broker = {"url": "http://127.0.0.1:1", "pid": 12345}
        sid = companion_module.broker_create_session(broker, "/tmp")
        assert sid is None


class TestBrokerAbortSession:
    def test_abort_success(self, companion_module, mock_broker):
        broker = {"url": mock_broker.url, "pid": 12345}
        result = companion_module.broker_abort_session(broker, "/tmp", "ses_123")
        assert result is True

    def test_no_broker(self, companion_module):
        result = companion_module.broker_abort_session(None, "/tmp", "ses_123")
        assert result is False

    def test_no_sid(self, companion_module, mock_broker):
        broker = {"url": mock_broker.url, "pid": 12345}
        result = companion_module.broker_abort_session(broker, "/tmp", None)
        assert result is False

    def test_unreachable(self, companion_module):
        broker = {"url": "http://127.0.0.1:1", "pid": 12345}
        result = companion_module.broker_abort_session(broker, "/tmp", "ses_123", timeout=1)
        assert result is False


class TestParseListenUrl:
    def test_finds_url(self, companion_module, tmp_path):
        log = tmp_path / "broker.log"
        log.write_text("some output\nopencode server listening on http://127.0.0.1:12345\nmore output\n")
        url = companion_module._parse_listen_url(log)
        assert url == "http://127.0.0.1:12345"

    def test_multiple_urls_returns_last(self, companion_module, tmp_path):
        log = tmp_path / "broker.log"
        log.write_text(
            "opencode server listening on http://127.0.0.1:11111\n"
            "opencode server listening on http://127.0.0.1:22222\n"
        )
        url = companion_module._parse_listen_url(log)
        assert url == "http://127.0.0.1:22222"

    def test_no_url(self, companion_module, tmp_path):
        log = tmp_path / "broker.log"
        log.write_text("no listening message here\n")
        url = companion_module._parse_listen_url(log)
        assert url is None

    def test_nonexistent_file(self, companion_module, tmp_path):
        url = companion_module._parse_listen_url(tmp_path / "nonexistent.log")
        assert url is None


class TestPidAlive:
    def test_current_pid(self, companion_module):
        assert companion_module._pid_alive(os.getpid()) is True

    def test_dead_pid(self, companion_module):
        # PID 1 is usually init, but let's use a definitely dead PID
        assert companion_module._pid_alive(999999) is False

    def test_none_pid(self, companion_module):
        assert companion_module._pid_alive(None) is False

    def test_zero_pid(self, companion_module):
        assert companion_module._pid_alive(0) is False

    def test_string_pid(self, companion_module):
        assert companion_module._pid_alive(str(os.getpid())) is True


class TestReadBroker:
    def test_reads_valid_broker(self, companion_module, workspace):
        broker = {"url": "http://127.0.0.1:12345", "pid": 12345, "startedAt": "2026-01-01T00:00:00Z"}
        bj, _, _ = companion_module._broker_paths(workspace)
        bj.parent.mkdir(parents=True, exist_ok=True)
        bj.write_text(json.dumps(broker))
        result = companion_module.read_broker(workspace)
        assert result["url"] == "http://127.0.0.1:12345"
        assert result["pid"] == 12345

    def test_missing_file(self, companion_module, workspace):
        result = companion_module.read_broker(workspace)
        assert result is None

    def test_corrupt_file(self, companion_module, workspace):
        bj, _, _ = companion_module._broker_paths(workspace)
        bj.parent.mkdir(parents=True, exist_ok=True)
        bj.write_text("not json")
        result = companion_module.read_broker(workspace)
        assert result is None

    def test_missing_url(self, companion_module, workspace):
        bj, _, _ = companion_module._broker_paths(workspace)
        bj.parent.mkdir(parents=True, exist_ok=True)
        bj.write_text(json.dumps({"pid": 12345}))
        result = companion_module.read_broker(workspace)
        assert result is None

    def test_missing_pid(self, companion_module, workspace):
        bj, _, _ = companion_module._broker_paths(workspace)
        bj.parent.mkdir(parents=True, exist_ok=True)
        bj.write_text(json.dumps({"url": "http://127.0.0.1:12345"}))
        result = companion_module.read_broker(workspace)
        assert result is None


class TestStopBroker:
    def test_no_broker(self, companion_module, workspace):
        result = companion_module.stop_broker(workspace)
        assert result is False

    def test_dead_broker(self, companion_module, workspace):
        broker = {"url": "http://127.0.0.1:12345", "pid": 999999, "startedAt": "2026-01-01T00:00:00Z"}
        bj, _, _ = companion_module._broker_paths(workspace)
        bj.parent.mkdir(parents=True, exist_ok=True)
        bj.write_text(json.dumps(broker))
        result = companion_module.stop_broker(workspace)
        # PID is dead, so nothing to stop, but file is cleaned up
        assert result is False
        assert not bj.exists()

    def test_removes_broker_file(self, companion_module, workspace):
        bj, _, _ = companion_module._broker_paths(workspace)
        bj.parent.mkdir(parents=True, exist_ok=True)
        bj.write_text(json.dumps({"url": "http://127.0.0.1:1", "pid": 999999}))
        companion_module.stop_broker(workspace)
        assert not bj.exists()


class TestBrokerPaths:
    def test_paths(self, companion_module, workspace):
        bj, blog, block = companion_module._broker_paths(workspace)
        assert bj.name == "broker.json"
        assert blog.name == "broker.log"
        assert block.name == "broker.lock"
        assert bj.parent == blog.parent == block.parent
