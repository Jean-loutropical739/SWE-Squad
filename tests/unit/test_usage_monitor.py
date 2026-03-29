"""Tests for the UsageMonitorProvider (JSONL-based)."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.swe_team.providers.usage_monitor.base import UsageEntry, UsageMonitorProvider
from src.swe_team.providers.usage_monitor.jsonl import JSONLUsageMonitor
from src.swe_team.providers.usage_monitor.pricing import (
    DEFAULT_PRICING,
    get_price,
    load_pricing,
    save_pricing,
    _normalize_model,
    _fuzzy_match,
)
from src.swe_team.providers.usage_monitor import create_provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_assistant_line(
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 0,
    cache_read: int = 0,
    timestamp: str | None = None,
    session_id: str = "sess-001",
) -> str:
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    obj = {
        "type": "assistant",
        "timestamp": timestamp,
        "sessionId": session_id,
        "message": {
            "model": model,
            "type": "message",
            "role": "assistant",
            "content": [],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
        "uuid": "test-uuid-1",
        "requestId": "req-001",
    }
    return json.dumps(obj)


def _write_jsonl(tmp: Path, lines: list[str], project: str = "test-project") -> Path:
    project_dir = tmp / "projects" / project
    project_dir.mkdir(parents=True, exist_ok=True)
    fp = project_dir / "session.jsonl"
    fp.write_text("\n".join(lines) + "\n")
    return fp


# ---------------------------------------------------------------------------
# Pricing tests
# ---------------------------------------------------------------------------

class TestPricing:
    def test_exact_match(self):
        assert get_price("claude-sonnet-4", "input") == 3.0
        assert get_price("claude-sonnet-4", "output") == 15.0

    def test_fuzzy_match_with_date(self):
        assert get_price("claude-opus-4-20250514", "input") == 15.0

    def test_fuzzy_match_with_context_suffix(self):
        assert get_price("claude-opus-4-6[1m]", "input") == 15.0

    def test_fuzzy_match_strips_subversion(self):
        assert get_price("claude-sonnet-4-6", "input") == 3.0

    def test_unknown_model_returns_zero(self):
        assert get_price("gpt-4o", "input") == 0.0

    def test_unknown_token_type_returns_zero(self):
        assert get_price("claude-sonnet-4", "nonexistent") == 0.0

    def test_cache_prices(self):
        assert get_price("claude-opus-4", "cache_write") == 18.75
        assert get_price("claude-opus-4", "cache_read") == 1.50

    def test_normalize_model(self):
        assert _normalize_model("Claude-Opus-4-20250514") == "claude-opus-4"
        assert _normalize_model("claude-opus-4-6[1m]") == "claude-opus-4-6"

    def test_save_and_load_pricing(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pricing.json")
            custom = {"my-model": {"input": 1.0, "output": 2.0}}
            save_pricing(custom, path)
            loaded = load_pricing(path)
            assert loaded == custom

    def test_load_missing_file_returns_defaults(self):
        loaded = load_pricing("/nonexistent/path/pricing.json")
        assert loaded == DEFAULT_PRICING

    def test_load_none_returns_defaults(self):
        loaded = load_pricing(None)
        assert isinstance(loaded, dict)


# ---------------------------------------------------------------------------
# JSONL parsing tests
# ---------------------------------------------------------------------------

class TestJSONLParsing:
    def test_valid_line(self):
        with tempfile.TemporaryDirectory() as td:
            lines = [_make_assistant_line(input_tokens=500, output_tokens=200)]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=1)
            assert len(entries) == 1
            assert entries[0].input_tokens == 500
            assert entries[0].output_tokens == 200

    def test_malformed_line_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            lines = [
                "this is not json",
                _make_assistant_line(input_tokens=100),
                '{"type": "assistant", "message": "not a dict"}',
                "",
            ]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=1)
            assert len(entries) == 1

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as td:
            _write_jsonl(Path(td), [""])
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=1)
            assert entries == []

    def test_non_assistant_lines_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            lines = [
                json.dumps({"type": "human", "timestamp": datetime.now(timezone.utc).isoformat()}),
                json.dumps({"type": "queue-operation", "timestamp": datetime.now(timezone.utc).isoformat()}),
                _make_assistant_line(),
            ]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=1)
            assert len(entries) == 1

    def test_missing_usage_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            line = json.dumps({
                "type": "assistant",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"model": "claude-sonnet-4", "content": []},
            })
            _write_jsonl(Path(td), [line])
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=1)
            assert entries == []


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

class TestCostCalculation:
    def test_cost_sonnet(self):
        with tempfile.TemporaryDirectory() as td:
            lines = [_make_assistant_line(
                model="claude-sonnet-4",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=1)
            assert len(entries) == 1
            # input: 3.0 + output: 15.0 = 18.0
            assert abs(entries[0].cost_usd - 18.0) < 0.01

    def test_cost_with_cache(self):
        with tempfile.TemporaryDirectory() as td:
            lines = [_make_assistant_line(
                model="claude-opus-4",
                input_tokens=0,
                output_tokens=0,
                cache_creation=1_000_000,
                cache_read=1_000_000,
            )]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=1)
            # cache_write: 18.75 + cache_read: 1.50 = 20.25
            assert abs(entries[0].cost_usd - 20.25) < 0.01

    def test_total_cost(self):
        with tempfile.TemporaryDirectory() as td:
            lines = [
                _make_assistant_line(model="claude-sonnet-4", input_tokens=1_000_000, output_tokens=0, session_id="s1"),
                _make_assistant_line(model="claude-sonnet-4", input_tokens=1_000_000, output_tokens=0, session_id="s2"),
            ]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            # Each: 3.0 USD input
            assert abs(monitor.total_cost(since_hours=1) - 6.0) < 0.01

    def test_unknown_model_zero_cost(self):
        with tempfile.TemporaryDirectory() as td:
            lines = [_make_assistant_line(model="gpt-4o", input_tokens=1000, output_tokens=1000)]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=1)
            assert entries[0].cost_usd == 0.0


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_duplicate_entries_removed(self):
        with tempfile.TemporaryDirectory() as td:
            ts = datetime.now(timezone.utc).isoformat()
            line = _make_assistant_line(timestamp=ts, session_id="s1", input_tokens=100, output_tokens=50)
            # Write the same line twice
            _write_jsonl(Path(td), [line, line])
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=1)
            assert len(entries) == 1

    def test_different_entries_kept(self):
        with tempfile.TemporaryDirectory() as td:
            lines = [
                _make_assistant_line(session_id="s1", input_tokens=100),
                _make_assistant_line(session_id="s2", input_tokens=200),
            ]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=1)
            assert len(entries) == 2


# ---------------------------------------------------------------------------
# Date filtering
# ---------------------------------------------------------------------------

class TestDateFiltering:
    def test_old_entries_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
            new_ts = datetime.now(timezone.utc).isoformat()
            lines = [
                _make_assistant_line(timestamp=old_ts, session_id="old"),
                _make_assistant_line(timestamp=new_ts, session_id="new"),
            ]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=24)
            assert len(entries) == 1
            assert entries[0].session_id == "new"

    def test_since_hours_zero_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            lines = [_make_assistant_line()]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            entries = monitor.load_usage(since_hours=0)
            assert entries == []


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_aggregate_by_day(self):
        with tempfile.TemporaryDirectory() as td:
            ts1 = datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc).isoformat()
            ts2 = datetime(2026, 3, 20, 14, 0, tzinfo=timezone.utc).isoformat()
            ts3 = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc).isoformat()
            lines = [
                _make_assistant_line(timestamp=ts1, session_id="s1", input_tokens=100),
                _make_assistant_line(timestamp=ts2, session_id="s2", input_tokens=200),
                _make_assistant_line(timestamp=ts3, session_id="s3", input_tokens=300),
            ]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            agg = monitor.aggregate_by("day", since_hours=24 * 365)
            assert "2026-03-20" in agg
            assert agg["2026-03-20"]["count"] == 2
            assert agg["2026-03-20"]["input_tokens"] == 300
            assert "2026-03-21" in agg
            assert agg["2026-03-21"]["count"] == 1

    def test_aggregate_by_hour(self):
        with tempfile.TemporaryDirectory() as td:
            ts = datetime(2026, 3, 20, 10, 30, tzinfo=timezone.utc).isoformat()
            lines = [_make_assistant_line(timestamp=ts, session_id="s1")]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            agg = monitor.aggregate_by("hour", since_hours=24 * 365)
            assert "2026-03-20 10:00" in agg

    def test_aggregate_by_month(self):
        with tempfile.TemporaryDirectory() as td:
            ts = datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc).isoformat()
            lines = [_make_assistant_line(timestamp=ts, session_id="s1")]
            _write_jsonl(Path(td), lines)
            monitor = JSONLUsageMonitor(config_dir=td)
            agg = monitor.aggregate_by("month", since_hours=24 * 365)
            assert "2026-03" in agg


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_jsonl_monitor_is_usage_provider(self):
        monitor = JSONLUsageMonitor(config_dir="/nonexistent")
        assert isinstance(monitor, UsageMonitorProvider)

    def test_factory(self):
        provider = create_provider({"config_dir": "/nonexistent"})
        assert isinstance(provider, JSONLUsageMonitor)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_health_check_with_files(self):
        with tempfile.TemporaryDirectory() as td:
            _write_jsonl(Path(td), [_make_assistant_line()])
            monitor = JSONLUsageMonitor(config_dir=td)
            assert monitor.health_check() is True

    def test_health_check_no_files(self):
        monitor = JSONLUsageMonitor(config_dir="/nonexistent")
        assert monitor.health_check() is False


# ---------------------------------------------------------------------------
# Project name derivation
# ---------------------------------------------------------------------------

class TestProjectName:
    def test_project_from_path(self):
        name = JSONLUsageMonitor._project_from_path(
            Path("/home/user/.claude/projects/-home-user-myproject/session.jsonl")
        )
        assert name == "home/user/myproject"
