"""
Tests for log_query providers: LocalFileProvider, LokiProvider,
and the factory/registry (__init__.py).

Covers:
  1. Protocol compliance (both implementations satisfy LogQueryProvider)
  2. LocalFileProvider — query_logs, search_logs, health_check
  3. LocalFileProvider — line parsing (text + JSON formats)
  4. LokiProvider — query building, response parsing, health_check
  5. LokiProvider — HTTP error handling (graceful failures)
  6. Config / construction tests
  7. Factory tests (create_log_query_provider)
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, mock_open, patch

import pytest

from src.swe_team.providers.log_query.base import LogEntry, LogQueryProvider
from src.swe_team.providers.log_query.local import LocalFileProvider
from src.swe_team.providers.log_query.loki import LokiProvider, _extract_level
from src.swe_team.providers.log_query import create_log_query_provider


# ======================================================================
# 1. Protocol compliance
# ======================================================================


class TestProtocolCompliance:
    """Both providers must satisfy the LogQueryProvider runtime protocol."""

    def test_local_provider_is_log_query_provider(self) -> None:
        provider = LocalFileProvider({"log_directories": []})
        assert isinstance(provider, LogQueryProvider)

    def test_loki_provider_is_log_query_provider(self) -> None:
        provider = LokiProvider({"loki_url": "http://localhost:3100"})
        assert isinstance(provider, LogQueryProvider)

    def test_protocol_has_required_methods(self) -> None:
        """Verify the protocol defines the three expected methods."""
        assert hasattr(LogQueryProvider, "query_logs")
        assert hasattr(LogQueryProvider, "search_logs")
        assert hasattr(LogQueryProvider, "health_check")


# ======================================================================
# 2. LogEntry dataclass
# ======================================================================


class TestLogEntry:
    """LogEntry is the shared data model for all providers."""

    def test_basic_construction(self) -> None:
        entry = LogEntry(
            timestamp="2024-01-15T12:00:00",
            level="ERROR",
            message="something broke",
            source="myapp",
        )
        assert entry.timestamp == "2024-01-15T12:00:00"
        assert entry.level == "ERROR"
        assert entry.message == "something broke"
        assert entry.source == "myapp"
        assert entry.metadata == {}

    def test_metadata_default(self) -> None:
        e1 = LogEntry(timestamp="", level="INFO", message="a", source="s")
        e2 = LogEntry(timestamp="", level="INFO", message="b", source="s")
        # Ensure default factory creates independent dicts
        e1.metadata["key"] = "val"
        assert "key" not in e2.metadata

    def test_metadata_custom(self) -> None:
        entry = LogEntry(
            timestamp="", level="INFO", message="m", source="s",
            metadata={"foo": "bar"},
        )
        assert entry.metadata == {"foo": "bar"}


# ======================================================================
# 3. LocalFileProvider — construction & config
# ======================================================================


class TestLocalFileProviderConfig:
    def test_defaults(self) -> None:
        p = LocalFileProvider({})
        assert p._log_directories == []
        assert p._remote_collection is False
        assert p._remote_local_dir == "logs/remote"
        assert p._file_pattern == "*.log"

    def test_custom_config(self) -> None:
        p = LocalFileProvider({
            "log_directories": ["/var/log/app"],
            "remote_collection": True,
            "remote_local_dir": "/tmp/remote",
            "file_pattern": "*.txt",
        })
        assert p._log_directories == ["/var/log/app"]
        assert p._remote_collection is True
        assert p._remote_local_dir == "/tmp/remote"
        assert p._file_pattern == "*.txt"


# ======================================================================
# 4. LocalFileProvider — line parsing
# ======================================================================


class TestLocalFileProviderParsing:
    """Test _parse_line against all supported formats."""

    def test_timestamp_level_format(self) -> None:
        line = "2024-01-15 12:34:56,789 [ERROR] Connection refused"
        entry = LocalFileProvider._parse_line(line, "app")
        assert entry is not None
        assert entry.timestamp == "2024-01-15 12:34:56,789"
        assert entry.level == "ERROR"
        assert entry.message == "Connection refused"
        assert entry.source == "app"

    def test_timestamp_level_format_iso(self) -> None:
        line = "2024-01-15T12:34:56 WARNING disk almost full"
        entry = LocalFileProvider._parse_line(line, "syslog")
        assert entry is not None
        assert entry.level == "WARNING"
        assert entry.message == "disk almost full"

    def test_bracket_level_format(self) -> None:
        line = "[ERROR] something failed"
        entry = LocalFileProvider._parse_line(line, "worker")
        assert entry is not None
        assert entry.timestamp == ""
        assert entry.level == "ERROR"
        assert entry.message == "something failed"

    def test_colon_level_format(self) -> None:
        line = "CRITICAL: system overload"
        entry = LocalFileProvider._parse_line(line, "monitor")
        assert entry is not None
        assert entry.level == "CRITICAL"
        assert entry.message == "system overload"

    def test_json_format(self) -> None:
        data = {
            "timestamp": "2024-01-15T12:00:00Z",
            "level": "error",
            "message": "db connection lost",
            "service": "api",
            "request_id": "abc123",
        }
        line = json.dumps(data)
        entry = LocalFileProvider._parse_line(line, "fallback_source")
        assert entry is not None
        assert entry.timestamp == "2024-01-15T12:00:00Z"
        assert entry.level == "ERROR"
        assert entry.message == "db connection lost"
        assert entry.source == "api"
        assert entry.metadata["request_id"] == "abc123"

    def test_json_format_alternative_keys(self) -> None:
        data = {"time": "2024-01-15", "severity": "warn", "msg": "slow query"}
        line = json.dumps(data)
        entry = LocalFileProvider._parse_line(line, "db")
        assert entry is not None
        assert entry.timestamp == "2024-01-15"
        assert entry.level == "WARN"
        assert entry.message == "slow query"
        # source falls back to "db" from function arg since no "source" key
        assert entry.source == "db"

    def test_unparseable_line(self) -> None:
        line = "just some random text without a level marker"
        entry = LocalFileProvider._parse_line(line, "unknown")
        assert entry is not None
        assert entry.level == "INFO"
        assert entry.message == line

    def test_empty_line_skipped_in_parse_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        log_file.write_text("\n\n[ERROR] real error\n\n")
        entries = LocalFileProvider({"log_directories": []})._parse_file(log_file)
        assert len(entries) == 1
        assert entries[0].level == "ERROR"

    def test_invalid_json_falls_through(self) -> None:
        line = '{"broken json'
        entry = LocalFileProvider._parse_line(line, "src")
        # Should fall through to unparseable
        assert entry is not None
        assert entry.level == "INFO"


# ======================================================================
# 5. LocalFileProvider — query_logs and search_logs
# ======================================================================


class TestLocalFileProviderQuery:
    """Test query_logs and search_logs with real temp files."""

    @pytest.fixture()
    def log_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "logs"
        d.mkdir()
        log_file = d / "app.log"
        log_file.write_text(
            "2024-01-15T12:00:01 ERROR db connection failed\n"
            "2024-01-15T12:00:02 INFO request handled\n"
            "2024-01-15T12:00:03 WARNING slow query detected\n"
            "2024-01-15T12:00:04 ERROR timeout on service X\n"
            "2024-01-15T12:00:05 DEBUG internal state dump\n"
        )
        # Touch file so mtime is recent
        os.utime(log_file, None)
        return d

    @pytest.fixture()
    def provider(self, log_dir: Path) -> LocalFileProvider:
        return LocalFileProvider({"log_directories": [str(log_dir)]})

    def test_query_logs_all(self, provider: LocalFileProvider) -> None:
        results = provider.query_logs(since_minutes=60)
        assert len(results) == 5

    def test_query_logs_filter_level(self, provider: LocalFileProvider) -> None:
        results = provider.query_logs(level="ERROR", since_minutes=60)
        assert len(results) == 2
        assert all(e.level == "ERROR" for e in results)

    def test_query_logs_filter_service(self, provider: LocalFileProvider) -> None:
        results = provider.query_logs(service="app", since_minutes=60)
        assert len(results) == 5  # all from "app.log" -> source="app"

    def test_query_logs_filter_service_no_match(self, provider: LocalFileProvider) -> None:
        results = provider.query_logs(service="nonexistent", since_minutes=60)
        assert len(results) == 0

    def test_query_logs_limit(self, provider: LocalFileProvider) -> None:
        results = provider.query_logs(limit=2, since_minutes=60)
        assert len(results) == 2

    def test_query_logs_sorted_newest_first(self, provider: LocalFileProvider) -> None:
        results = provider.query_logs(since_minutes=60)
        timestamps = [e.timestamp for e in results if e.timestamp]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_search_logs_regex(self, provider: LocalFileProvider) -> None:
        results = provider.search_logs(pattern=r"timeout.*service", since_minutes=60)
        assert len(results) == 1
        assert "timeout" in results[0].message.lower()

    def test_search_logs_substring_fallback_on_bad_regex(self, provider: LocalFileProvider) -> None:
        # Invalid regex should fall back to substring match
        results = provider.search_logs(pattern="[invalid regex", since_minutes=60)
        # No entries contain "[invalid regex" literally
        assert len(results) == 0

    def test_search_logs_with_service_filter(self, provider: LocalFileProvider) -> None:
        results = provider.search_logs(pattern="slow", service="app", since_minutes=60)
        assert len(results) == 1

    def test_query_logs_skips_old_files(self, tmp_path: Path) -> None:
        d = tmp_path / "old_logs"
        d.mkdir()
        log_file = d / "old.log"
        log_file.write_text("[ERROR] ancient error\n")
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(log_file, (old_time, old_time))

        provider = LocalFileProvider({"log_directories": [str(d)]})
        results = provider.query_logs(since_minutes=60)
        assert len(results) == 0

    def test_query_logs_skips_nonexistent_dir(self) -> None:
        provider = LocalFileProvider({"log_directories": ["/nonexistent/path"]})
        results = provider.query_logs(since_minutes=60)
        assert results == []

    def test_search_logs_case_insensitive_regex(self, provider: LocalFileProvider) -> None:
        results = provider.search_logs(pattern="DB CONNECTION", since_minutes=60)
        assert len(results) == 1  # matches "db connection failed"


# ======================================================================
# 6. LocalFileProvider — health_check
# ======================================================================


class TestLocalFileProviderHealthCheck:
    def test_health_check_passes_when_dir_exists(self, tmp_path: Path) -> None:
        d = tmp_path / "logs"
        d.mkdir()
        p = LocalFileProvider({"log_directories": [str(d)]})
        assert p.health_check() is True

    def test_health_check_fails_when_no_dirs_exist(self) -> None:
        p = LocalFileProvider({"log_directories": ["/nonexistent/a", "/nonexistent/b"]})
        assert p.health_check() is False

    def test_health_check_fails_when_no_dirs_configured(self) -> None:
        p = LocalFileProvider({"log_directories": []})
        assert p.health_check() is False


# ======================================================================
# 7. LocalFileProvider — remote collection
# ======================================================================


class TestLocalFileProviderRemoteCollection:
    def test_remote_collection_disabled_by_default(self, tmp_path: Path) -> None:
        d = tmp_path / "logs"
        d.mkdir()
        p = LocalFileProvider({"log_directories": [str(d)]})
        # Should not call collect_remote_logs
        with patch("src.swe_team.providers.log_query.local.LocalFileProvider._maybe_collect_remote") as mock_collect:
            p.query_logs(since_minutes=60)
            # _maybe_collect_remote IS called, but internally it returns early
        # Directly test the internal
        p2 = LocalFileProvider({"log_directories": [str(d)], "remote_collection": False})
        with patch("src.swe_team.remote_logs.collect_remote_logs") as mock_rl:
            p2._maybe_collect_remote()
            mock_rl.assert_not_called()

    def test_remote_collection_enabled_calls_collect(self) -> None:
        p = LocalFileProvider({
            "log_directories": [],
            "remote_collection": True,
            "remote_local_dir": "/tmp/remote",
        })
        with patch("src.swe_team.remote_logs.collect_remote_logs", return_value=["/tmp/remote/worker1"]) as mock_rl:
            p._maybe_collect_remote()
            mock_rl.assert_called_once_with(local_dir="/tmp/remote")
            assert "/tmp/remote/worker1" in p._log_directories

    def test_remote_collection_deduplicates_dirs(self) -> None:
        p = LocalFileProvider({
            "log_directories": ["/existing/dir"],
            "remote_collection": True,
        })
        with patch("src.swe_team.remote_logs.collect_remote_logs", return_value=["/existing/dir", "/new/dir"]):
            p._maybe_collect_remote()
            assert p._log_directories.count("/existing/dir") == 1
            assert "/new/dir" in p._log_directories

    def test_remote_collection_error_handled_gracefully(self) -> None:
        p = LocalFileProvider({
            "log_directories": [],
            "remote_collection": True,
        })
        with patch("src.swe_team.remote_logs.collect_remote_logs", side_effect=RuntimeError("ssh failed")):
            # Should not raise
            p._maybe_collect_remote()
            assert p._log_directories == []


# ======================================================================
# 8. LocalFileProvider — file parsing edge cases
# ======================================================================


class TestLocalFileProviderEdgeCases:
    def test_parse_file_unreadable(self, tmp_path: Path) -> None:
        log_file = tmp_path / "unreadable.log"
        log_file.write_text("data")
        log_file.chmod(0o000)
        provider = LocalFileProvider({"log_directories": []})
        entries = provider._parse_file(log_file)
        # Should return empty, not raise
        assert entries == []
        # Restore permissions for cleanup
        log_file.chmod(0o644)

    def test_custom_file_pattern(self, tmp_path: Path) -> None:
        d = tmp_path / "logs"
        d.mkdir()
        (d / "app.log").write_text("[ERROR] in log\n")
        (d / "app.txt").write_text("[ERROR] in txt\n")
        os.utime(d / "app.log", None)
        os.utime(d / "app.txt", None)

        p = LocalFileProvider({"log_directories": [str(d)], "file_pattern": "*.txt"})
        results = p.query_logs(since_minutes=60)
        assert len(results) == 1
        assert results[0].source == "app"

    def test_multiple_log_directories(self, tmp_path: Path) -> None:
        d1 = tmp_path / "logs1"
        d2 = tmp_path / "logs2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "a.log").write_text("[ERROR] from dir1\n")
        (d2 / "b.log").write_text("[WARNING] from dir2\n")
        os.utime(d1 / "a.log", None)
        os.utime(d2 / "b.log", None)

        p = LocalFileProvider({"log_directories": [str(d1), str(d2)]})
        results = p.query_logs(since_minutes=60)
        assert len(results) == 2
        sources = {e.source for e in results}
        assert sources == {"a", "b"}


# ======================================================================
# 9. LokiProvider — construction & config
# ======================================================================


class TestLokiProviderConfig:
    def test_defaults(self) -> None:
        p = LokiProvider({})
        assert p._loki_url == "http://localhost:3100"
        assert p._timeout == 10

    def test_custom_config(self) -> None:
        p = LokiProvider({
            "loki_url": "http://loki.internal:3100/",
            "timeout_seconds": 30,
        })
        # Trailing slash is stripped
        assert p._loki_url == "http://loki.internal:3100"
        assert p._timeout == 30


# ======================================================================
# 10. LokiProvider — query building
# ======================================================================


class TestLokiProviderQueryBuilding:
    def test_build_query_no_filters(self) -> None:
        q = LokiProvider._build_query()
        assert q == '{job=~".+"}'

    def test_build_query_service_only(self) -> None:
        q = LokiProvider._build_query(service="api")
        assert q == '{service="api"}'

    def test_build_query_service_and_level(self) -> None:
        q = LokiProvider._build_query(service="api", level="error")
        assert q == '{service="api"} |= `ERROR`'

    def test_build_query_pattern_only(self) -> None:
        q = LokiProvider._build_query(pattern="timeout")
        assert q == '{job=~".+"} |~ `timeout`'

    def test_build_query_service_and_pattern(self) -> None:
        q = LokiProvider._build_query(service="worker", pattern="connection.*refused")
        assert q == '{service="worker"} |~ `connection.*refused`'

    def test_build_query_all_filters(self) -> None:
        q = LokiProvider._build_query(service="api", level="ERROR", pattern="db")
        assert q == '{service="api"} |= `ERROR` |~ `db`'


# ======================================================================
# 11. LokiProvider — response parsing
# ======================================================================


class TestLokiProviderResponseParsing:
    def test_parse_empty_response(self) -> None:
        data: Dict[str, Any] = {"data": {"result": []}}
        entries = LokiProvider._parse_response(data)
        assert entries == []

    def test_parse_missing_data_key(self) -> None:
        entries = LokiProvider._parse_response({})
        assert entries == []

    def test_parse_single_stream(self) -> None:
        data = {
            "data": {
                "result": [
                    {
                        "stream": {"service": "api", "level": "error"},
                        "values": [
                            ["1705312800000000000", "db connection failed"],
                            ["1705312801000000000", "retrying connection"],
                        ],
                    }
                ]
            }
        }
        entries = LokiProvider._parse_response(data)
        assert len(entries) == 2
        assert entries[0].source == "api"
        assert entries[0].level == "ERROR"
        assert entries[0].message == "db connection failed"
        assert "T" in entries[0].timestamp  # ISO format

    def test_parse_multiple_streams(self) -> None:
        data = {
            "data": {
                "result": [
                    {
                        "stream": {"service": "api"},
                        "values": [["1705312800000000000", "request handled"]],
                    },
                    {
                        "stream": {"job": "worker"},
                        "values": [["1705312800000000000", "task completed"]],
                    },
                ]
            }
        }
        entries = LokiProvider._parse_response(data)
        assert len(entries) == 2
        sources = {e.source for e in entries}
        assert sources == {"api", "worker"}

    def test_parse_skips_short_values(self) -> None:
        data = {
            "data": {
                "result": [
                    {
                        "stream": {"service": "api"},
                        "values": [
                            ["only_one_element"],
                            ["1705312800000000000", "valid entry"],
                        ],
                    }
                ]
            }
        }
        entries = LokiProvider._parse_response(data)
        assert len(entries) == 1

    def test_parse_invalid_timestamp(self) -> None:
        data = {
            "data": {
                "result": [
                    {
                        "stream": {"service": "api"},
                        "values": [["not_a_number", "some message"]],
                    }
                ]
            }
        }
        entries = LokiProvider._parse_response(data)
        assert len(entries) == 1
        assert entries[0].timestamp == "not_a_number"

    def test_parse_labels_stored_in_metadata(self) -> None:
        data = {
            "data": {
                "result": [
                    {
                        "stream": {"service": "api", "env": "prod", "region": "us-east"},
                        "values": [["1705312800000000000", "log line"]],
                    }
                ]
            }
        }
        entries = LokiProvider._parse_response(data)
        assert entries[0].metadata["env"] == "prod"
        assert entries[0].metadata["region"] == "us-east"

    def test_parse_uses_job_as_source_fallback(self) -> None:
        data = {
            "data": {
                "result": [
                    {
                        "stream": {"job": "cron_worker"},
                        "values": [["1705312800000000000", "tick"]],
                    }
                ]
            }
        }
        entries = LokiProvider._parse_response(data)
        assert entries[0].source == "cron_worker"

    def test_parse_unknown_source_fallback(self) -> None:
        data = {
            "data": {
                "result": [
                    {
                        "stream": {},
                        "values": [["1705312800000000000", "orphan log"]],
                    }
                ]
            }
        }
        entries = LokiProvider._parse_response(data)
        assert entries[0].source == "unknown"


# ======================================================================
# 12. LokiProvider — _extract_level helper
# ======================================================================


class TestExtractLevel:
    def test_extracts_error(self) -> None:
        assert _extract_level("Something ERROR happened") == "ERROR"

    def test_extracts_critical(self) -> None:
        assert _extract_level("CRITICAL: disk full") == "CRITICAL"

    def test_extracts_warning(self) -> None:
        assert _extract_level("WARNING: disk 90%") == "WARNING"

    def test_warn_normalized_to_warning(self) -> None:
        assert _extract_level("WARN slow query") == "WARNING"

    def test_extracts_debug(self) -> None:
        assert _extract_level("DEBUG internal state") == "DEBUG"

    def test_default_info(self) -> None:
        assert _extract_level("just a regular message") == "INFO"

    def test_case_insensitive(self) -> None:
        assert _extract_level("error in lowercase") == "ERROR"

    def test_priority_critical_over_error(self) -> None:
        # CRITICAL is checked first in the loop
        assert _extract_level("CRITICAL ERROR scenario") == "CRITICAL"


# ======================================================================
# 13. LokiProvider — query_logs and search_logs (HTTP mocked)
# ======================================================================


class TestLokiProviderQueryExecution:
    @pytest.fixture()
    def provider(self) -> LokiProvider:
        return LokiProvider({"loki_url": "http://loki.test:3100", "timeout_seconds": 5})

    def _mock_urlopen(self, response_data: Dict[str, Any]) -> MagicMock:
        """Create a mock urlopen context manager returning JSON data."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_query_logs_success(self, provider: LokiProvider) -> None:
        response_data = {
            "data": {
                "result": [
                    {
                        "stream": {"service": "api", "level": "error"},
                        "values": [["1705312800000000000", "error happened"]],
                    }
                ]
            }
        }
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(response_data)):
            results = provider.query_logs(service="api", level="ERROR", since_minutes=30, limit=100)
            assert len(results) == 1
            assert results[0].message == "error happened"

    def test_search_logs_success(self, provider: LokiProvider) -> None:
        response_data = {
            "data": {
                "result": [
                    {
                        "stream": {"service": "worker"},
                        "values": [["1705312800000000000", "timeout on connection"]],
                    }
                ]
            }
        }
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(response_data)):
            results = provider.search_logs(pattern="timeout", service="worker", since_minutes=60)
            assert len(results) == 1

    def test_query_logs_network_error_returns_empty(self, provider: LokiProvider) -> None:
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("unreachable")):
            results = provider.query_logs(since_minutes=60)
            assert results == []

    def test_query_logs_timeout_returns_empty(self, provider: LokiProvider) -> None:
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            results = provider.query_logs(since_minutes=60)
            assert results == []

    def test_search_logs_error_returns_empty(self, provider: LokiProvider) -> None:
        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            results = provider.search_logs(pattern="test")
            assert results == []


# ======================================================================
# 14. LokiProvider — health_check
# ======================================================================


class TestLokiProviderHealthCheck:
    def test_health_check_success(self) -> None:
        p = LokiProvider({"loki_url": "http://loki.test:3100"})
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert p.health_check() is True

    def test_health_check_failure_unreachable(self) -> None:
        p = LokiProvider({"loki_url": "http://loki.test:3100"})
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            assert p.health_check() is False

    def test_health_check_failure_timeout(self) -> None:
        p = LokiProvider({"loki_url": "http://loki.test:3100"})
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            assert p.health_check() is False

    def test_health_check_calls_correct_endpoint(self) -> None:
        p = LokiProvider({"loki_url": "http://loki.test:3100"})
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            p.health_check()
            call_args = mock_open.call_args
            req = call_args[0][0]
            assert req.full_url == "http://loki.test:3100/loki/api/v1/ready"
            assert req.method == "GET"


# ======================================================================
# 15. Factory — create_log_query_provider
# ======================================================================


class TestFactory:
    def test_none_config_returns_none(self) -> None:
        assert create_log_query_provider(None) is None

    def test_default_provider_is_local(self) -> None:
        p = create_log_query_provider({"log_directories": ["/tmp"]})
        assert isinstance(p, LocalFileProvider)

    def test_explicit_local_provider(self) -> None:
        p = create_log_query_provider({"provider": "local", "log_directories": []})
        assert isinstance(p, LocalFileProvider)

    def test_explicit_loki_provider(self) -> None:
        p = create_log_query_provider({
            "provider": "loki",
            "loki_url": "http://loki:3100",
        })
        assert isinstance(p, LokiProvider)

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown log_query provider"):
            create_log_query_provider({"provider": "elasticsearch"})

    def test_factory_result_satisfies_protocol(self) -> None:
        p = create_log_query_provider({"provider": "local", "log_directories": []})
        assert isinstance(p, LogQueryProvider)

        p2 = create_log_query_provider({"provider": "loki"})
        assert isinstance(p2, LogQueryProvider)

    def test_factory_passes_config_to_local(self) -> None:
        p = create_log_query_provider({
            "provider": "local",
            "log_directories": ["/var/log"],
            "file_pattern": "*.txt",
        })
        assert isinstance(p, LocalFileProvider)
        assert p._log_directories == ["/var/log"]
        assert p._file_pattern == "*.txt"

    def test_factory_passes_config_to_loki(self) -> None:
        p = create_log_query_provider({
            "provider": "loki",
            "loki_url": "http://custom:9090",
            "timeout_seconds": 25,
        })
        assert isinstance(p, LokiProvider)
        assert p._loki_url == "http://custom:9090"
        assert p._timeout == 25
