"""Unit tests for LokiProvider — Grafana Loki log query provider."""
from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.log_query.base import LogEntry, LogQueryProvider
from src.swe_team.providers.log_query.loki import LokiProvider
from src.swe_team.providers.log_query import create_log_query_provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _loki_response(streams: list) -> Dict[str, Any]:
    """Build a minimal Loki query_range JSON response."""
    return {"status": "success", "data": {"resultType": "streams", "result": streams}}


def _mock_urlopen(response_body: bytes, status: int = 200):
    """Return a context-manager mock for urllib.request.urlopen."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_body
    mock_resp.status = status
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


SAMPLE_STREAM = {
    "stream": {"service": "api-gateway", "level": "error", "job": "myapp"},
    "values": [
        ["1700000000000000000", "Connection refused to database"],
        ["1700000001000000000", "Retrying connection attempt 2"],
    ],
}

SAMPLE_STREAM_NO_LEVEL = {
    "stream": {"job": "worker", "namespace": "prod"},
    "values": [
        ["1700000002000000000", "INFO Starting batch processing"],
        ["1700000003000000000", "ERROR Disk write failed"],
    ],
}


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_loki_provider_is_protocol_instance(self):
        provider = LokiProvider({"loki_url": "http://localhost:3100"})
        assert isinstance(provider, LogQueryProvider)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestFactory:
    def test_loki_factory(self):
        p = create_log_query_provider({
            "provider": "loki",
            "loki_url": "http://loki.local:3100",
        })
        assert isinstance(p, LokiProvider)

    def test_loki_config_from_yaml_style(self):
        config = {
            "provider": "loki",
            "loki_url": "http://localhost:3100",
            "timeout_seconds": 15,
        }
        p = LokiProvider(config)
        assert p._loki_url == "http://localhost:3100"
        assert p._timeout == 15

    def test_default_config(self):
        p = LokiProvider({})
        assert p._loki_url == "http://localhost:3100"
        assert p._timeout == 10


# ---------------------------------------------------------------------------
# LogQL query building
# ---------------------------------------------------------------------------

class TestQueryBuilding:
    def test_no_filters(self):
        q = LokiProvider._build_query()
        assert q == '{job=~".+"}'

    def test_service_filter(self):
        q = LokiProvider._build_query(service="api-gateway")
        assert q == '{service="api-gateway"}'

    def test_level_filter(self):
        q = LokiProvider._build_query(level="error")
        assert "|= `ERROR`" in q

    def test_service_and_level(self):
        q = LokiProvider._build_query(service="api", level="ERROR")
        assert q == '{service="api"} |= `ERROR`'

    def test_pattern_filter(self):
        q = LokiProvider._build_query(pattern="timeout")
        assert '|~ `timeout`' in q

    def test_service_and_pattern(self):
        q = LokiProvider._build_query(service="worker", pattern="disk.*fail")
        assert '{service="worker"}' in q
        assert '|~ `disk.*fail`' in q

    def test_all_filters(self):
        q = LokiProvider._build_query(service="api", level="ERROR", pattern="conn")
        assert '{service="api"}' in q
        assert '|= `ERROR`' in q
        assert '|~ `conn`' in q


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

class TestResponseParsing:
    def test_parse_streams(self):
        data = _loki_response([SAMPLE_STREAM])
        entries = LokiProvider._parse_response(data)
        assert len(entries) == 2
        assert all(isinstance(e, LogEntry) for e in entries)
        assert entries[0].source == "api-gateway"
        assert entries[0].level == "ERROR"
        assert "Connection refused" in entries[0].message

    def test_parse_level_from_labels(self):
        data = _loki_response([SAMPLE_STREAM])
        entries = LokiProvider._parse_response(data)
        # Level comes from stream labels
        assert all(e.level == "ERROR" for e in entries)

    def test_parse_level_from_line_when_no_label(self):
        data = _loki_response([SAMPLE_STREAM_NO_LEVEL])
        entries = LokiProvider._parse_response(data)
        assert entries[0].level == "INFO"
        assert entries[1].level == "ERROR"

    def test_parse_source_fallback_to_job(self):
        stream = {
            "stream": {"job": "batch-worker"},
            "values": [["1700000000000000000", "Processing item 42"]],
        }
        entries = LokiProvider._parse_response(_loki_response([stream]))
        assert entries[0].source == "batch-worker"

    def test_parse_empty_response(self):
        entries = LokiProvider._parse_response(_loki_response([]))
        assert entries == []

    def test_parse_timestamp_conversion(self):
        data = _loki_response([SAMPLE_STREAM])
        entries = LokiProvider._parse_response(data)
        # Should be ISO format
        assert "2023-11-14" in entries[0].timestamp

    def test_metadata_contains_labels(self):
        data = _loki_response([SAMPLE_STREAM])
        entries = LokiProvider._parse_response(data)
        assert entries[0].metadata.get("job") == "myapp"

    def test_short_value_skipped(self):
        stream = {
            "stream": {"job": "x"},
            "values": [["only_one_element"]],
        }
        entries = LokiProvider._parse_response(_loki_response([stream]))
        assert entries == []


# ---------------------------------------------------------------------------
# query_logs with mocked HTTP
# ---------------------------------------------------------------------------

class TestQueryLogs:
    @patch("src.swe_team.providers.log_query.loki.urllib.request.urlopen")
    def test_query_logs_returns_entries(self, mock_urlopen):
        body = json.dumps(_loki_response([SAMPLE_STREAM])).encode()
        mock_urlopen.return_value = _mock_urlopen(body)

        p = LokiProvider({"loki_url": "http://loki:3100"})
        entries = p.query_logs(service="api-gateway", level="ERROR", since_minutes=30)
        assert len(entries) == 2
        mock_urlopen.assert_called_once()

    @patch("src.swe_team.providers.log_query.loki.urllib.request.urlopen")
    def test_query_logs_passes_limit(self, mock_urlopen):
        body = json.dumps(_loki_response([])).encode()
        mock_urlopen.return_value = _mock_urlopen(body)

        p = LokiProvider({"loki_url": "http://loki:3100"})
        p.query_logs(limit=42)
        url = mock_urlopen.call_args[0][0].full_url
        assert "limit=42" in url


# ---------------------------------------------------------------------------
# search_logs with mocked HTTP
# ---------------------------------------------------------------------------

class TestSearchLogs:
    @patch("src.swe_team.providers.log_query.loki.urllib.request.urlopen")
    def test_search_logs(self, mock_urlopen):
        body = json.dumps(_loki_response([SAMPLE_STREAM_NO_LEVEL])).encode()
        mock_urlopen.return_value = _mock_urlopen(body)

        p = LokiProvider({"loki_url": "http://loki:3100"})
        entries = p.search_logs("batch", service="worker")
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# Timeout / unreachable handling
# ---------------------------------------------------------------------------

class TestGracefulFailure:
    @patch("src.swe_team.providers.log_query.loki.urllib.request.urlopen")
    def test_timeout_returns_empty(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("timed out")
        p = LokiProvider({"loki_url": "http://loki:3100", "timeout_seconds": 1})
        entries = p.query_logs()
        assert entries == []

    @patch("src.swe_team.providers.log_query.loki.urllib.request.urlopen")
    def test_connection_refused_returns_empty(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionRefusedError("Connection refused")
        p = LokiProvider({"loki_url": "http://loki:3100"})
        entries = p.query_logs()
        assert entries == []

    @patch("src.swe_team.providers.log_query.loki.urllib.request.urlopen")
    def test_search_unreachable_returns_empty(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("Network unreachable")
        p = LokiProvider({"loki_url": "http://loki:3100"})
        entries = p.search_logs("error")
        assert entries == []


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealthCheck:
    @patch("src.swe_team.providers.log_query.loki.urllib.request.urlopen")
    def test_healthy(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(b"ready", status=200)
        p = LokiProvider({"loki_url": "http://loki:3100"})
        assert p.health_check() is True

    @patch("src.swe_team.providers.log_query.loki.urllib.request.urlopen")
    def test_unhealthy_on_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("refused")
        p = LokiProvider({"loki_url": "http://loki:3100"})
        assert p.health_check() is False

    @patch("src.swe_team.providers.log_query.loki.urllib.request.urlopen")
    def test_health_check_calls_ready_endpoint(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(b"ready", status=200)
        p = LokiProvider({"loki_url": "http://loki:3100"})
        p.health_check()
        url = mock_urlopen.call_args[0][0].full_url
        assert "/loki/api/v1/ready" in url
