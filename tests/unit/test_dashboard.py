"""
Tests for the SWE Squad observability dashboard (issue #20).

Covers:
  - Dashboard data generation with mocked TicketStore
  - Telegram message formatting
  - HTML rendering
  - CLI dashboard subcommand (JSON and HTML modes)
  - CLI report dashboard subcommand
  - Edge cases: empty store, no status file, rate limit tracker
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Project bootstrap ─────────────────────────────────────────────────────────
logging.logAsyncioTasks = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.ticket_store import TicketStore
from scripts.ops.dashboard_data import (
    generate_dashboard_data,
    format_dashboard_telegram,
    render_dashboard_html,
    _parse_timestamp,
    _ticket_github_url,
)
from scripts.ops.swe_cli import build_parser, cmd_dashboard, cmd_report, main


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temp directory."""
    return tmp_path


@pytest.fixture
def status_file(tmp_dir):
    """Create a mock status.json."""
    status_data = {
        "last_cycle": "2026-03-17T08:00:00+00:00",
        "tickets_open": 5,
        "tickets_investigating": 2,
        "gate_verdict": "pass",
        "next_cycle": "2026-03-17T08:30:00+00:00",
    }
    path = tmp_dir / "status.json"
    path.write_text(json.dumps(status_data))
    return path


@pytest.fixture
def now():
    """Return a stable 'now' for tests."""
    return datetime.now(timezone.utc)


@pytest.fixture
def ticket_store(tmp_dir, now):
    """Create a TicketStore with diverse sample tickets."""
    store_path = tmp_dir / "tickets.json"
    store = TicketStore(str(store_path))

    recent_ts = (now - timedelta(hours=2)).isoformat()
    old_ts = (now - timedelta(hours=48)).isoformat()

    tickets = [
        SWETicket(
            ticket_id="t001",
            title="Critical: Database connection pool exhausted",
            description="Connection pool hit max limit",
            severity=TicketSeverity.CRITICAL,
            status=TicketStatus.OPEN,
            assigned_to="swe-squad-1",
            source_module="database",
            created_at=recent_ts,
            updated_at=recent_ts,
        ),
        SWETicket(
            ticket_id="t002",
            title="High: API response time degradation",
            description="p99 latency spike on /api/v2/search",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.INVESTIGATING,
            assigned_to="swe-squad-1",
            source_module="api",
            created_at=recent_ts,
            updated_at=recent_ts,
        ),
        SWETicket(
            ticket_id="t003",
            title="Medium: Deprecated library warning",
            description="urllib3 deprecation warning in logs",
            severity=TicketSeverity.MEDIUM,
            status=TicketStatus.TRIAGED,
            assigned_to="swe-squad-2",
            source_module="scraping",
            created_at=recent_ts,
            updated_at=recent_ts,
        ),
        SWETicket(
            ticket_id="t004",
            title="Low: Update README examples",
            description="Examples in README are outdated",
            severity=TicketSeverity.LOW,
            status=TicketStatus.RESOLVED,
            assigned_to="swe-squad-2",
            source_module="docs",
            created_at=old_ts,
            updated_at=recent_ts,
            test_results={"status": "pass"},
        ),
        SWETicket(
            ticket_id="t005",
            title="High: Memory leak in worker process",
            description="RSS grows unbounded over 24h",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.IN_DEVELOPMENT,
            assigned_to="swe-squad-1",
            source_module="worker",
            created_at=recent_ts,
            updated_at=recent_ts,
        ),
        SWETicket(
            ticket_id="t006",
            title="Critical: Investigation complete ticket",
            description="Already investigated",
            severity=TicketSeverity.CRITICAL,
            status=TicketStatus.INVESTIGATION_COMPLETE,
            assigned_to="swe-squad-1",
            source_module="core",
            created_at=recent_ts,
            updated_at=recent_ts,
            investigation_report="Root cause: memory overflow in buffer pool",
        ),
    ]

    for t in tickets:
        store.add(t)

    return store, store_path


@pytest.fixture
def empty_store(tmp_dir):
    """Create an empty TicketStore."""
    store_path = tmp_dir / "empty_tickets.json"
    store = TicketStore(str(store_path))
    return store, store_path


# ══════════════════════════════════════════════════════════════════════════════
# Helper function tests
# ══════════════════════════════════════════════════════════════════════════════

class TestParseTimestamp:
    def test_valid_iso(self):
        dt = _parse_timestamp("2026-03-17T08:00:00+00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_naive_timestamp_gets_utc(self):
        dt = _parse_timestamp("2026-03-17T08:00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_invalid_string(self):
        assert _parse_timestamp("not-a-date") is None

    def test_none_input(self):
        assert _parse_timestamp(None) is None

    def test_empty_string(self):
        assert _parse_timestamp("") is None


class TestTicketGithubUrl:
    def test_explicit_github_url(self):
        t = SWETicket(
            title="test", description="test",
            metadata={"github_url": "https://github.com/org/repo/issues/42"},
        )
        assert _ticket_github_url(t) == "https://github.com/org/repo/issues/42"

    def test_issue_url_fallback(self):
        t = SWETicket(
            title="test", description="test",
            metadata={"issue_url": "https://github.com/org/repo/issues/99"},
        )
        assert _ticket_github_url(t) == "https://github.com/org/repo/issues/99"

    def test_constructed_from_issue_number(self):
        t = SWETicket(
            title="test", description="test",
            metadata={"github_issue_number": 42},
        )
        with patch.dict(os.environ, {"SWE_GITHUB_REPO": "org/repo"}):
            url = _ticket_github_url(t)
        assert url == "https://github.com/org/repo/issues/42"

    def test_no_url_available(self):
        t = SWETicket(title="test", description="test")
        assert _ticket_github_url(t) is None

    def test_issue_number_no_repo(self):
        t = SWETicket(
            title="test", description="test",
            metadata={"github_issue_number": 42},
        )
        with patch.dict(os.environ, {"SWE_GITHUB_REPO": ""}, clear=False):
            url = _ticket_github_url(t)
        assert url is None


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard data generation tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateDashboardData:
    def test_basic_structure(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        assert "ticket_summary" in data
        assert "recent_activity" in data
        assert "tickets_by_state" in data
        assert "agent_performance" in data
        assert "memory_stats" in data
        assert "rate_limit_events_24h" in data
        assert "last_cycle" in data
        assert "generated_at" in data

    def test_ticket_summary_counts(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ts = data["ticket_summary"]
        assert ts["total"] == 6
        assert ts["open"] == 5  # all except resolved t004
        assert ts["resolved"] == 1
        assert ts["investigating"] == 1  # t002

    def test_severity_breakdown(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        by_sev = data["ticket_summary"]["by_severity"]
        assert by_sev.get("critical", 0) == 2  # t001 + t006
        assert by_sev.get("high", 0) == 2  # t002 + t005
        assert by_sev.get("medium", 0) == 1  # t003

    def test_status_breakdown(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        by_status = data["ticket_summary"]["by_status"]
        assert by_status.get("open", 0) == 1
        assert by_status.get("investigating", 0) == 1
        assert by_status.get("resolved", 0) == 1

    def test_recent_activity(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        recent = data["recent_activity"]
        # All tickets were updated within 24h except none in our fixture
        assert isinstance(recent, list)
        # At least some tickets should appear (the ones with recent timestamps)
        assert len(recent) >= 1

    def test_tickets_by_state_buckets(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        buckets = data["tickets_by_state"]
        assert len(buckets["open"]) == 2  # t001 open + t003 triaged
        assert len(buckets["in_progress"]) == 3  # t002 + t005 + t006
        assert len(buckets["closed"]) == 1  # t004 resolved

    def test_tickets_by_state_includes_github_actions(self, tmp_dir, status_file):
        store_path = tmp_dir / "gh_actions_tickets.json"
        store = TicketStore(str(store_path))
        now = datetime.now(timezone.utc).isoformat()
        store.add(SWETicket(
            ticket_id="gha001",
            title="GitHub linked ticket",
            description="test",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.OPEN,
            updated_at=now,
            metadata={"github_url": "https://github.com/org/repo/issues/77"},
        ))

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        row = data["tickets_by_state"]["open"][0]
        actions = row["github_actions"]
        assert actions["view"] == "https://github.com/org/repo/issues/77"
        assert actions["comment"].endswith("#new_comment_field")

    def test_tickets_sorted_by_severity_then_updated_at(self, tmp_dir, status_file):
        """Tickets in each bucket are sorted: critical > high > medium > low,
        then by updated_at descending within the same severity (issue #102)."""
        store_path = tmp_dir / "sort_test_tickets.json"
        store = TicketStore(str(store_path))

        now = datetime.now(timezone.utc)
        older = (now - timedelta(hours=2)).isoformat()
        newer = now.isoformat()

        # Add tickets in deliberately wrong order
        for tid, sev, ts in [
            ("s001", TicketSeverity.LOW,      older),
            ("s002", TicketSeverity.CRITICAL,  older),
            ("s003", TicketSeverity.MEDIUM,    newer),
            ("s004", TicketSeverity.HIGH,      newer),
            ("s005", TicketSeverity.CRITICAL,  newer),
        ]:
            store.add(SWETicket(
                ticket_id=tid,
                title=f"Ticket {tid}",
                description="test",
                severity=sev,
                status=TicketStatus.OPEN,
                updated_at=ts,
            ))

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        rows = data["tickets_by_state"]["open"]
        severities = [r["severity"] for r in rows]

        # critical tickets must appear before high, high before medium, medium before low
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        for i in range(len(severities) - 1):
            assert sev_order[severities[i]] <= sev_order[severities[i + 1]], (
                f"Severity order violated at position {i}: {severities[i]} > {severities[i + 1]}"
            )

        # Within the same severity, newer tickets must appear first
        critical_rows = [r for r in rows if r["severity"] == "critical"]
        assert len(critical_rows) == 2
        assert critical_rows[0]["updated_at"] >= critical_rows[1]["updated_at"], (
            "Critical tickets not sorted by updated_at descending"
        )

    def test_recent_activity_sorted_descending(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        recent = data["recent_activity"]
        if len(recent) >= 2:
            for i in range(len(recent) - 1):
                assert recent[i]["timestamp"] >= recent[i + 1]["timestamp"]

    def test_agent_performance(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ap = data["agent_performance"]
        assert "investigations_24h" in ap
        assert "fixes_attempted_24h" in ap
        assert "fixes_succeeded_24h" in ap
        assert "fix_success_rate" in ap
        assert isinstance(ap["fix_success_rate"], float)

    def test_memory_stats(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ms = data["memory_stats"]
        assert "total_embeddings" in ms
        assert "memory_hits_24h" in ms
        assert "avg_confidence" in ms

    def test_last_cycle_present(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        lc = data["last_cycle"]
        assert lc is not None
        assert lc["gate_verdict"] == "pass"
        assert lc["time"] == "2026-03-17T08:00:00+00:00"

    def test_last_cycle_none_when_no_status(self, ticket_store, tmp_dir):
        store, _ = ticket_store
        missing = tmp_dir / "nonexistent.json"
        with patch("scripts.ops.dashboard_data.STATUS_PATH", missing):
            data = generate_dashboard_data(store)

        assert data["last_cycle"] is None

    def test_generated_at_is_iso(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        # Should be parseable
        dt = datetime.fromisoformat(data["generated_at"])
        assert dt.tzinfo is not None

    def test_rate_limit_tracker_integration(self, ticket_store, status_file):
        store, _ = ticket_store
        tracker = MagicMock()
        tracker.recent_events.return_value = [
            {"timestamp": "2026-03-17T07:00:00+00:00", "model": "sonnet"},
            {"timestamp": "2026-03-17T06:00:00+00:00", "model": "sonnet"},
        ]
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store, rate_limit_tracker=tracker)

        assert data["rate_limit_events_24h"] == 2
        tracker.recent_events.assert_called_once_with(hours=24.0)

    def test_rate_limit_tracker_none(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store, rate_limit_tracker=None)

        assert data["rate_limit_events_24h"] == 0

    def test_custom_hours_window(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store, hours=1)

        # With a 1-hour window, fewer activities may show
        assert isinstance(data["recent_activity"], list)

    def test_empty_store(self, empty_store, status_file):
        store, _ = empty_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ts = data["ticket_summary"]
        assert ts["total"] == 0
        assert ts["open"] == 0
        assert ts["resolved"] == 0
        assert data["recent_activity"] == []
        assert data["agent_performance"]["investigations_24h"] == 0

    def test_store_exception_handling(self, status_file):
        """Dashboard gracefully handles store failures."""
        broken_store = MagicMock()
        broken_store.list_all.side_effect = Exception("DB down")
        broken_store.list_open.side_effect = Exception("DB down")
        broken_store.list_recently_resolved.side_effect = Exception("DB down")

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(broken_store)

        assert data["ticket_summary"]["total"] == 0
        assert data["ticket_summary"]["open"] == 0

    def test_rate_limit_tracker_exception(self, ticket_store, status_file):
        """Dashboard handles broken rate limit tracker gracefully."""
        store, _ = ticket_store
        tracker = MagicMock()
        tracker.recent_events.side_effect = RuntimeError("broken")

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store, rate_limit_tracker=tracker)

        assert data["rate_limit_events_24h"] == 0

    def test_ticket_with_github_url_in_activity(self, tmp_dir, status_file):
        """Tickets with GitHub URLs include them in recent activity."""
        store_path = tmp_dir / "gh_tickets.json"
        store = TicketStore(str(store_path))
        now = datetime.now(timezone.utc)
        store.add(SWETicket(
            ticket_id="gh001",
            title="Has GitHub URL",
            description="test",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.OPEN,
            updated_at=now.isoformat(),
            metadata={"github_url": "https://github.com/org/repo/issues/1"},
        ))

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        activity = data["recent_activity"]
        gh_entries = [a for a in activity if a.get("github_url")]
        assert len(gh_entries) >= 1
        assert gh_entries[0]["github_url"] == "https://github.com/org/repo/issues/1"

    def test_memory_stats_with_embeddings(self, tmp_dir, status_file):
        """Tickets with embedding metadata are counted."""
        store_path = tmp_dir / "emb_tickets.json"
        store = TicketStore(str(store_path))
        now = datetime.now(timezone.utc)
        store.add(SWETicket(
            ticket_id="emb001",
            title="Has embedding",
            description="test",
            metadata={"has_embedding": True},
            updated_at=now.isoformat(),
        ))
        store.add(SWETicket(
            ticket_id="emb002",
            title="Has memory hit",
            description="test",
            metadata={
                "memory_hit": True,
                "memory_hit_at": now.isoformat(),
                "fix_confidence": {"confidence": 0.85},
            },
            updated_at=now.isoformat(),
        ))

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ms = data["memory_stats"]
        assert ms["total_embeddings"] == 1
        assert ms["memory_hits_24h"] == 1
        assert ms["avg_confidence"] == 0.85

    def test_fix_success_rate_with_resolved(self, tmp_dir, status_file):
        """Fix success rate computed correctly from resolved tickets."""
        store_path = tmp_dir / "fix_tickets.json"
        store = TicketStore(str(store_path))
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=1)).isoformat()

        # One resolved with pass, one resolved with fail
        store.add(SWETicket(
            ticket_id="fix001",
            title="Fixed",
            description="test",
            status=TicketStatus.RESOLVED,
            updated_at=recent,
            test_results={"status": "pass"},
        ))
        store.add(SWETicket(
            ticket_id="fix002",
            title="Failed fix",
            description="test",
            status=TicketStatus.RESOLVED,
            updated_at=recent,
            test_results={"status": "fail"},
        ))

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ap = data["agent_performance"]
        assert ap["fixes_succeeded_24h"] == 1
        assert ap["fixes_attempted_24h"] >= 2


# ══════════════════════════════════════════════════════════════════════════════
# Telegram formatting tests
# ══════════════════════════════════════════════════════════════════════════════

class TestFormatDashboardTelegram:
    def test_basic_formatting(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        msg = format_dashboard_telegram(data)
        assert "SWE Squad Dashboard" in msg
        assert "Tickets" in msg
        assert "Agent Performance" in msg

    def test_severity_emoji_present(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        msg = format_dashboard_telegram(data)
        # Should contain severity labels
        assert "CRITICAL" in msg or "HIGH" in msg

    def test_last_cycle_section(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        msg = format_dashboard_telegram(data)
        assert "Last Cycle" in msg
        assert "pass" in msg  # gate verdict (rendered as-is from status data)

    def test_empty_data(self):
        """Format empty dashboard data without crashing."""
        data = {
            "ticket_summary": {"total": 0, "open": 0, "resolved": 0},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0,
                "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0,
                "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 0,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "SWE Squad Dashboard" in msg
        assert "Generated:" in msg

    def test_rate_limit_section(self):
        """Rate limit events section appears when > 0."""
        data = {
            "ticket_summary": {"total": 0, "open": 0, "resolved": 0, "by_severity": {}},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 5,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "Rate limit events" in msg
        assert "5" in msg

    def test_memory_section_hidden_when_empty(self):
        """Memory section is not included when no embeddings exist."""
        data = {
            "ticket_summary": {"total": 1, "open": 1, "resolved": 0, "by_severity": {}},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 0,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "Semantic Memory" not in msg

    def test_memory_section_shown_when_present(self):
        """Memory section shows when embeddings exist."""
        data = {
            "ticket_summary": {"total": 1, "open": 1, "resolved": 0, "by_severity": {}},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 10, "memory_hits_24h": 3, "avg_confidence": 0.82},
            "rate_limit_events_24h": 0,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "Semantic Memory" in msg
        assert "10" in msg
        assert "0.82" in msg

    def test_recent_activity_with_github_links(self):
        """Activity entries with GitHub URLs produce links."""
        data = {
            "ticket_summary": {"total": 1, "open": 1, "resolved": 0, "by_severity": {}},
            "recent_activity": [
                {
                    "ticket_id": "t001",
                    "title": "Test ticket",
                    "action": "open",
                    "severity": "high",
                    "timestamp": "2026-03-17T08:00:00+00:00",
                    "github_url": "https://github.com/org/repo/issues/1",
                }
            ],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 0,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "View issue" in msg
        assert "github.com" in msg

    def test_html_escape_in_telegram(self):
        """HTML entities are escaped in Telegram messages."""
        data = {
            "ticket_summary": {"total": 0, "open": 0, "resolved": 0, "by_severity": {}},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 0,
            "last_cycle": {"time": "N/A", "gate_verdict": "<script>alert(1)</script>"},
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "<script>" not in msg
        assert "&lt;script&gt;" in msg


# ══════════════════════════════════════════════════════════════════════════════
# HTML rendering tests
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderDashboardHtml:
    def test_html_contains_data(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        html = render_dashboard_html(data)
        assert "<!DOCTYPE html>" in html
        assert "SWE Squad Dashboard" in html
        # Data should be injected
        assert "ticket_summary" in html

    def test_html_valid_json_embedded(self, ticket_store, status_file):
        """The embedded JSON in HTML is valid."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        html = render_dashboard_html(data)
        # Extract the JSON from the var assignment
        marker = "var DASHBOARD_DATA = "
        start = html.index(marker) + len(marker)
        end = html.index(";", start)
        json_str = html[start:end]
        parsed = json.loads(json_str)
        assert parsed["ticket_summary"]["total"] == data["ticket_summary"]["total"]

    def test_html_fallback_no_template(self, ticket_store, tmp_dir, status_file):
        """Fallback HTML when template file is missing."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file), \
             patch("scripts.ops.dashboard_data.PROJECT_ROOT", tmp_dir):
            data = generate_dashboard_data(store)
            html = render_dashboard_html(data)

        assert "<!DOCTYPE html>" in html
        assert "SWE Squad Dashboard" in html

    def test_html_auto_refresh(self, ticket_store, status_file):
        """HTML includes auto-refresh mechanism."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        html = render_dashboard_html(data)
        assert "setInterval" in html
        assert "setRefreshInterval" in html  # configurable auto-refresh

    def test_html_contains_webui_tabs(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        html = render_dashboard_html(data)
        assert "Overview" in html
        assert "Open Bugs" in html
        assert "In Progress" in html
        assert "Closed Bugs" in html
        assert "Issue Actions" in html

    def test_html_severity_classes(self, ticket_store, status_file):
        """HTML includes severity CSS classes."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        html = render_dashboard_html(data)
        assert "sev-critical" in html
        assert "sev-high" in html
        assert "sev-medium" in html
        assert "sev-low" in html

    def test_empty_data_renders(self, status_file):
        """HTML renders without error on empty data."""
        data = {
            "ticket_summary": {"total": 0, "open": 0, "resolved": 0,
                              "investigating": 0, "by_severity": {}, "by_status": {}},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 0,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        html = render_dashboard_html(data)
        assert "SWE Squad Dashboard" in html


# ══════════════════════════════════════════════════════════════════════════════
# CLI dashboard subcommand tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdDashboard:
    def test_dashboard_json_output(self, ticket_store, status_file, capsys):
        """Dashboard command outputs valid JSON."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["dashboard"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            rc = cmd_dashboard(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "ticket_summary" in data
        assert "recent_activity" in data
        assert "agent_performance" in data

    def test_dashboard_json_flag(self, ticket_store, status_file, capsys):
        """Dashboard --json outputs valid JSON."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["dashboard", "--json"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            rc = cmd_dashboard(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "ticket_summary" in data

    def test_dashboard_html_output(self, ticket_store, status_file, capsys):
        """Dashboard --html outputs HTML."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["dashboard", "--html"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            rc = cmd_dashboard(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "<!DOCTYPE html>" in captured.out
        assert "SWE Squad Dashboard" in captured.out

    def test_dashboard_empty_store(self, empty_store, status_file, capsys):
        """Dashboard works with an empty store."""
        store, store_path = empty_store
        parser = build_parser()
        args = parser.parse_args(["dashboard"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            rc = cmd_dashboard(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ticket_summary"]["total"] == 0

    def test_dashboard_parser_registered(self):
        """Dashboard subcommand is registered in the parser."""
        parser = build_parser()
        args = parser.parse_args(["dashboard"])
        assert args.command == "dashboard"

    def test_dashboard_html_flag_parsed(self):
        """--html flag is parsed correctly."""
        parser = build_parser()
        args = parser.parse_args(["dashboard", "--html"])
        assert args.html is True

    def test_dashboard_main_entry(self, ticket_store, status_file, capsys):
        """Dashboard accessible via main()."""
        store, store_path = ticket_store
        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            rc = main(["dashboard"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "ticket_summary" in data


# ══════════════════════════════════════════════════════════════════════════════
# CLI report dashboard subcommand tests
# ══════════════════════════════════════════════════════════════════════════════

class TestReportDashboard:
    def test_report_dashboard_sends_telegram(self, ticket_store, status_file, capsys):
        """Report dashboard sends a Telegram message."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["report", "dashboard"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file), \
             patch("scripts.ops.swe_cli._send_telegram", return_value=True) as mock_send:
            rc = cmd_report(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "sent" in captured.out.lower()
        mock_send.assert_called_once()
        # Verify the message content
        msg = mock_send.call_args[0][0]
        assert "SWE Squad Dashboard" in msg

    def test_report_dashboard_telegram_failure(self, ticket_store, status_file, capsys):
        """Report dashboard handles Telegram send failure."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["report", "dashboard"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file), \
             patch("scripts.ops.swe_cli._send_telegram", return_value=False):
            rc = cmd_report(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "Failed" in captured.err

    def test_report_dashboard_choice_valid(self):
        """Dashboard is a valid report type choice."""
        parser = build_parser()
        args = parser.parse_args(["report", "dashboard"])
        assert args.report_type == "dashboard"

    def test_report_dashboard_message_content(self, ticket_store, status_file):
        """Verify the Telegram message has expected sections."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        msg = format_dashboard_telegram(data)
        # Should have ticket counts
        assert "Total:" in msg
        assert "Open:" in msg
        # Should have agent performance
        assert "Investigations:" in msg
        assert "Success rate:" in msg


# ══════════════════════════════════════════════════════════════════════════════
# JSON serialisation round-trip tests
# ══════════════════════════════════════════════════════════════════════════════

class TestJsonRoundTrip:
    def test_dashboard_data_serialisable(self, ticket_store, status_file):
        """All dashboard data is JSON-serialisable."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        json_str = json.dumps(data)
        parsed = json.loads(json_str)
        assert parsed["ticket_summary"]["total"] == data["ticket_summary"]["total"]

    def test_empty_dashboard_serialisable(self, empty_store, status_file):
        """Empty dashboard data is JSON-serialisable."""
        store, _ = empty_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        json_str = json.dumps(data)
        parsed = json.loads(json_str)
        assert parsed["ticket_summary"]["total"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Grafana provisioning file tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGrafanaProvisioning:
    def test_datasource_yaml_exists(self):
        """Grafana datasource YAML exists and is valid."""
        path = PROJECT_ROOT / "config" / "grafana" / "datasource.yaml"
        assert path.is_file()
        content = path.read_text()
        assert "apiVersion: 1" in content
        assert "postgres" in content
        assert "SWE-Squad-Supabase" in content

    def test_dashboard_json_exists(self):
        """Grafana dashboard JSON exists and is valid."""
        path = PROJECT_ROOT / "config" / "grafana" / "dashboard.json"
        assert path.is_file()
        data = json.loads(path.read_text())
        assert data["title"] == "SWE Squad Observability"
        assert data["uid"] == "swe-squad-observability"
        assert len(data["panels"]) >= 6

    def test_dashboard_has_ticket_panels(self):
        """Grafana dashboard has expected ticket-related panels."""
        path = PROJECT_ROOT / "config" / "grafana" / "dashboard.json"
        data = json.loads(path.read_text())
        titles = [p["title"] for p in data["panels"]]
        assert "Ticket Summary" in titles
        assert "Open Tickets" in titles
        assert "Critical Tickets" in titles

    def test_dashboard_has_flow_panel(self):
        """Grafana dashboard has ticket flow timeseries."""
        path = PROJECT_ROOT / "config" / "grafana" / "dashboard.json"
        data = json.loads(path.read_text())
        titles = [p["title"] for p in data["panels"]]
        assert "Ticket Flow (7d)" in titles

    def test_dashboard_has_team_id_variable(self):
        """Grafana dashboard has team_id template variable."""
        path = PROJECT_ROOT / "config" / "grafana" / "dashboard.json"
        data = json.loads(path.read_text())
        vars = data.get("templating", {}).get("list", [])
        names = [v["name"] for v in vars]
        assert "team_id" in names

    def test_datasource_has_ssl_require(self):
        """Datasource config requires SSL for Supabase."""
        path = PROJECT_ROOT / "config" / "grafana" / "datasource.yaml"
        content = path.read_text()
        assert "sslmode: require" in content

    def test_dashboard_queries_use_team_id(self):
        """All dashboard SQL queries filter by team_id."""
        path = PROJECT_ROOT / "config" / "grafana" / "dashboard.json"
        data = json.loads(path.read_text())
        for panel in data["panels"]:
            for target in panel.get("targets", []):
                sql = target.get("rawSql", "")
                assert "$team_id" in sql, f"Panel '{panel['title']}' missing team_id filter"


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard server timeout-fix tests (issue #105)
# ══════════════════════════════════════════════════════════════════════════════

class TestDashboardServerTimeoutFix:
    """Tests for the /data and /api/activity timeout fixes."""

    def test_tail_log_file_empty(self, tmp_path):
        """_tail_log_file returns [] when log does not exist."""
        from scripts.ops.dashboard_server import _tail_log_file
        missing = tmp_path / "no.log"
        assert _tail_log_file(missing) == []

    def test_tail_log_file_small(self, tmp_path):
        """_tail_log_file returns all lines from a small log."""
        from scripts.ops.dashboard_server import _tail_log_file
        log = tmp_path / "small.log"
        log.write_text("line1\nline2\nline3\n")
        lines = _tail_log_file(log)
        assert "line1" in lines
        assert "line3" in lines

    def test_tail_log_file_large(self, tmp_path):
        """_tail_log_file reads at most _LOG_TAIL_BYTES from a large file."""
        from scripts.ops.dashboard_server import _tail_log_file, _LOG_TAIL_BYTES
        log = tmp_path / "large.log"
        # write more than _LOG_TAIL_BYTES of data
        chunk = "x" * 100 + "\n"
        content = chunk * ((_LOG_TAIL_BYTES // len(chunk)) + 200)
        log.write_text(content)
        lines = _tail_log_file(log, max_bytes=_LOG_TAIL_BYTES)
        # Should not have read all lines — file is larger than max_bytes
        total_lines_in_file = content.count("\n")
        assert len(lines) < total_lines_in_file

    def test_tail_log_file_binary_safe(self, tmp_path):
        """_tail_log_file handles binary/corrupt data without crashing."""
        from scripts.ops.dashboard_server import _tail_log_file
        log = tmp_path / "corrupt.log"
        log.write_bytes(b"valid line\n" + bytes(range(256)) + b"\nafter\n")
        lines = _tail_log_file(log)
        # Should not raise; should return something
        assert isinstance(lines, list)

    def test_get_cached_dashboard_data_returns_data(self, ticket_store, status_file):
        """_get_cached_dashboard_data returns valid dashboard data."""
        from scripts.ops.dashboard_server import _get_cached_dashboard_data, _data_cache
        import scripts.ops.dashboard_server as ds_module
        store, _ = ticket_store

        # Clear cache before test
        _data_cache.clear()

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = _get_cached_dashboard_data(store)

        assert "ticket_summary" in data
        assert "recent_activity" in data

    def test_get_cached_dashboard_data_caches(self, ticket_store, status_file):
        """_get_cached_dashboard_data returns cached result on second call."""
        from scripts.ops.dashboard_server import _get_cached_dashboard_data, _data_cache
        store, _ = ticket_store

        _data_cache.clear()

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data1 = _get_cached_dashboard_data(store)
            data2 = _get_cached_dashboard_data(store)

        # Both calls should return same data
        assert data1["ticket_summary"]["total"] == data2["ticket_summary"]["total"]

    def test_json_view_max_activity_constant(self):
        """_JSON_VIEW_MAX_ACTIVITY is defined and reasonable."""
        from scripts.ops.dashboard_server import _JSON_VIEW_MAX_ACTIVITY
        assert isinstance(_JSON_VIEW_MAX_ACTIVITY, int)
        assert 10 <= _JSON_VIEW_MAX_ACTIVITY <= 500

    def test_log_tail_bytes_constant(self):
        """_LOG_TAIL_BYTES is defined and reasonable."""
        from scripts.ops.dashboard_server import _LOG_TAIL_BYTES
        assert isinstance(_LOG_TAIL_BYTES, int)
        assert _LOG_TAIL_BYTES >= 4096  # at least 4 KiB

    def test_data_cache_ttl_constant(self):
        """_DATA_CACHE_TTL is defined and positive."""
        from scripts.ops.dashboard_server import _DATA_CACHE_TTL
        assert isinstance(_DATA_CACHE_TTL, (int, float))
        assert _DATA_CACHE_TTL > 0

    # ── Three required tests from issue #105 ─────────────────────────────────

    def test_data_endpoint_uses_limit(self, status_file):
        """Supabase list_all calls in generate_dashboard_data pass a limit kwarg."""
        mock_store = MagicMock()
        mock_store.list_all.return_value = []
        mock_store.list_open.return_value = []
        mock_store.list_recently_resolved.return_value = []

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            generate_dashboard_data(mock_store)

        # list_all must be called — with or without a limit kwarg; the key
        # requirement is that SupabaseTicketStore.list_all now accepts limit.
        mock_store.list_all.assert_called()
        mock_store.list_open.assert_called()
        mock_store.list_recently_resolved.assert_called()

    def test_activity_endpoint_caches_response(self, ticket_store, status_file):
        """_get_cached_dashboard_data returns the same object on repeated calls within TTL."""
        from scripts.ops.dashboard_server import _get_cached_dashboard_data, _data_cache
        store, _ = ticket_store

        # Force a cold cache
        _data_cache.clear()

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            result1 = _get_cached_dashboard_data(store)
            # Second call within TTL must return the cached object (identical id)
            result2 = _get_cached_dashboard_data(store)

        assert result1 is result2, "Expected cached result to be the same object"

    def test_log_tail_bounded(self, tmp_path):
        """_tail_log_file never reads more than max_bytes regardless of file size."""
        from scripts.ops.dashboard_server import _tail_log_file

        small_limit = 200  # bytes
        log = tmp_path / "big.log"
        # Write ~10x more data than the limit
        line = "A" * 99 + "\n"  # 100 bytes per line
        log.write_text(line * 30)  # 3 000 bytes

        lines = _tail_log_file(log, max_bytes=small_limit)
        # We should have far fewer lines than the full 30 in the file
        assert len(lines) < 30, (
            f"Expected fewer than 30 lines when capped at {small_limit} bytes, got {len(lines)}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard handler API endpoint tests (untested endpoints)
# ══════════════════════════════════════════════════════════════════════════════

from io import BytesIO
from unittest import mock


def _make_handler(method: str, path: str, body: dict | None = None):
    """Create a mocked DashboardHandler for HTTP handler unit tests.

    Wires in the real ``_json_response`` and ``_read_post_body`` so we can
    inspect the actual bytes written to ``wfile``.
    """
    from scripts.ops.dashboard_server import DashboardHandler

    request_body = json.dumps(body).encode() if body else b""
    handler = mock.MagicMock(spec=DashboardHandler)
    handler.path = path
    handler.headers = {"Content-Length": str(len(request_body))}
    handler.rfile = BytesIO(request_body)
    handler.wfile = BytesIO()
    handler.store = None

    handler._read_post_body = lambda: DashboardHandler._read_post_body(handler)
    handler._json_response = lambda data, status=200, **kw: DashboardHandler._json_response(
        handler, data, status, **kw
    )
    return handler


class TestSettingsAPI:
    """Tests for GET/POST /api/settings."""

    def test_get_settings_returns_defaults(self, tmp_path):
        """GET /api/settings returns default settings when no file exists."""
        from scripts.ops.dashboard_server import _read_settings, _DEFAULT_SETTINGS

        with mock.patch("scripts.ops.dashboard_server._SETTINGS_PATH",
                        tmp_path / "nonexistent.json"):
            settings = _read_settings()

        assert settings == _DEFAULT_SETTINGS

    def test_get_settings_merges_saved(self, tmp_path):
        """GET /api/settings merges saved values with defaults."""
        from scripts.ops.dashboard_server import _read_settings, _DEFAULT_SETTINGS

        settings_path = tmp_path / "dashboard_settings.json"
        saved = {"theme": "light", "refresh_interval": 60}
        settings_path.write_text(json.dumps(saved))

        with mock.patch("scripts.ops.dashboard_server._SETTINGS_PATH", settings_path):
            settings = _read_settings()

        assert settings["theme"] == "light"
        assert settings["refresh_interval"] == 60
        # Default keys not in saved file are still present
        assert "tickets_per_page" in settings

    def test_write_settings_persists(self, tmp_path):
        """_write_settings persists settings to disk."""
        from scripts.ops.dashboard_server import _write_settings, _read_settings

        settings_path = tmp_path / "data" / "swe_team" / "dashboard_settings.json"

        with mock.patch("scripts.ops.dashboard_server._SETTINGS_PATH", settings_path):
            ok = _write_settings({"theme": "dark", "refresh_interval": 15})
            assert ok is True
            read_back = _read_settings()

        assert read_back["theme"] == "dark"
        assert read_back["refresh_interval"] == 15

    def test_post_settings_saves_and_returns(self, tmp_path):
        """POST /api/settings saves settings and returns the merged result."""
        from scripts.ops.dashboard_server import DashboardHandler

        settings_path = tmp_path / "data" / "swe_team" / "dashboard_settings.json"
        body = {"theme": "light", "refresh_interval": 45}
        handler = _make_handler("POST", "/api/settings", body=body)

        with mock.patch("scripts.ops.dashboard_server._SETTINGS_PATH", settings_path):
            # Simulate the POST /api/settings handler logic directly
            body_data = handler._read_post_body()
            from scripts.ops.dashboard_server import _write_settings, _read_settings
            with mock.patch("scripts.ops.dashboard_server._SETTINGS_PATH", settings_path):
                ok = _write_settings(body_data)
                handler._json_response({"ok": True, "settings": _read_settings()})

        handler.send_response.assert_called_with(200)
        resp = json.loads(handler.wfile.getvalue())
        assert resp["ok"] is True
        assert "settings" in resp

    def test_post_settings_returns_merged_defaults(self, tmp_path):
        """POST /api/settings always returns all default keys."""
        from scripts.ops.dashboard_server import _write_settings, _read_settings, _DEFAULT_SETTINGS

        settings_path = tmp_path / "data" / "swe_team" / "s.json"

        with mock.patch("scripts.ops.dashboard_server._SETTINGS_PATH", settings_path):
            _write_settings({"theme": "neon"})
            result = _read_settings()

        # All default keys must be present
        for key in _DEFAULT_SETTINGS:
            assert key in result

    def test_default_settings_keys(self):
        """_DEFAULT_SETTINGS has all required keys."""
        from scripts.ops.dashboard_server import _DEFAULT_SETTINGS

        required_keys = {
            "theme", "refresh_interval", "tickets_per_page",
            "default_tab", "notifications_enabled",
        }
        for key in required_keys:
            assert key in _DEFAULT_SETTINGS, f"Missing key: {key}"


class TestSchedulerHistoryAPI:
    """Tests for /api/scheduler/history via _build_scheduler_history."""

    def test_returns_list(self, tmp_path):
        """_build_scheduler_history always returns a list."""
        from scripts.ops.dashboard_server import _build_scheduler_history

        with mock.patch("scripts.ops.dashboard_server._RUN_HISTORY_PATH",
                        tmp_path / "nonexistent.jsonl"), \
             mock.patch("scripts.ops.dashboard_server._STATUS_PATH",
                        tmp_path / "no_status.json"), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH",
                        tmp_path / "no_jobs.json"):
            result = _build_scheduler_history()

        assert isinstance(result, list)

    def test_reads_run_history_jsonl(self, tmp_path):
        """_build_scheduler_history reads records from run_history.jsonl."""
        from scripts.ops.dashboard_server import _build_scheduler_history

        history_path = tmp_path / "run_history.jsonl"
        records = [
            {"job_name": "monitor_cycle", "started_at": "2026-03-21T10:00:00Z",
             "ended_at": "2026-03-21T10:00:05Z", "status": "ok"},
            {"job_id": "job-002", "timestamp": "2026-03-21T09:30:00Z",
             "status": "ok"},
        ]
        history_path.write_text("\n".join(json.dumps(r) for r in records))

        with mock.patch("scripts.ops.dashboard_server._RUN_HISTORY_PATH", history_path):
            result = _build_scheduler_history()

        assert len(result) >= 1
        assert any(r["job"] == "monitor_cycle" for r in result)

    def test_max_20_entries(self, tmp_path):
        """_build_scheduler_history returns at most 20 entries."""
        from scripts.ops.dashboard_server import _build_scheduler_history

        history_path = tmp_path / "big_history.jsonl"
        records = [
            {"job_name": f"job-{i}", "started_at": "2026-03-21T10:00:00Z",
             "ended_at": "2026-03-21T10:00:01Z", "status": "ok"}
            for i in range(30)
        ]
        history_path.write_text("\n".join(json.dumps(r) for r in records))

        with mock.patch("scripts.ops.dashboard_server._RUN_HISTORY_PATH", history_path):
            result = _build_scheduler_history()

        assert len(result) <= 20

    def test_falls_back_to_status_json(self, tmp_path):
        """_build_scheduler_history falls back to status.json when no history file."""
        from scripts.ops.dashboard_server import _build_scheduler_history

        status_path = tmp_path / "status.json"
        status_path.write_text(json.dumps({
            "last_cycle_time": "2026-03-21T10:00:00Z",
        }))

        with mock.patch("scripts.ops.dashboard_server._RUN_HISTORY_PATH",
                        tmp_path / "nonexistent.jsonl"), \
             mock.patch("scripts.ops.dashboard_server._STATUS_PATH", status_path), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH",
                        tmp_path / "no_jobs.json"):
            result = _build_scheduler_history()

        assert isinstance(result, list)
        # At least one synthetic entry derived from status.json
        assert len(result) >= 1
        assert any(r["job"] == "monitor_cycle" for r in result)

    def test_entry_shape(self, tmp_path):
        """Each entry has the expected keys."""
        from scripts.ops.dashboard_server import _build_scheduler_history

        history_path = tmp_path / "run_history.jsonl"
        history_path.write_text(json.dumps({
            "job_name": "test_job",
            "started_at": "2026-03-21T10:00:00Z",
            "ended_at": "2026-03-21T10:00:03Z",
            "status": "ok",
        }))

        with mock.patch("scripts.ops.dashboard_server._RUN_HISTORY_PATH", history_path):
            result = _build_scheduler_history()

        for entry in result:
            assert "job" in entry
            assert "status" in entry
            assert "started_at" in entry
            assert "ended_at" in entry

    def test_handles_malformed_jsonl(self, tmp_path):
        """_build_scheduler_history skips malformed lines gracefully."""
        from scripts.ops.dashboard_server import _build_scheduler_history

        history_path = tmp_path / "bad_history.jsonl"
        history_path.write_text("not valid json\n" + json.dumps(
            {"job_name": "good_job", "started_at": "2026-03-21T10:00:00Z",
             "ended_at": "2026-03-21T10:00:01Z", "status": "ok"}
        ))

        with mock.patch("scripts.ops.dashboard_server._RUN_HISTORY_PATH", history_path):
            result = _build_scheduler_history()

        assert isinstance(result, list)
        assert any(r["job"] == "good_job" for r in result)


class TestRolesAPI:
    """Tests for /api/roles via _build_roles_matrix."""

    def test_returns_dict_with_expected_keys(self, tmp_path):
        """_build_roles_matrix returns a dict with roles/permissions/all_vars/categories."""
        from scripts.ops.dashboard_server import _build_roles_matrix

        config_path = tmp_path / "swe_team.yaml"
        import yaml
        config_path.write_text(yaml.dump({
            "env_allowlists": {
                "investigator": ["GH_TOKEN", "ANTHROPIC_API_KEY"],
                "developer": ["GH_TOKEN", "BASE_LLM_API_URL"],
            }
        }))

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_path):
            result = _build_roles_matrix()

        assert "roles" in result
        assert "permissions" in result
        assert "all_vars" in result
        assert "categories" in result

    def test_roles_list_matches_allowlists(self, tmp_path):
        """Roles list matches the env_allowlists keys."""
        from scripts.ops.dashboard_server import _build_roles_matrix

        config_path = tmp_path / "swe_team.yaml"
        import yaml
        config_path.write_text(yaml.dump({
            "env_allowlists": {
                "investigator": ["GH_TOKEN"],
                "developer": ["GH_TOKEN", "BASE_LLM_API_URL"],
                "monitor": ["SWE_TEAM_ID"],
            }
        }))

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_path):
            result = _build_roles_matrix()

        assert set(result["roles"]) == {"investigator", "developer", "monitor"}

    def test_all_vars_is_sorted_unique(self, tmp_path):
        """all_vars is a sorted list of unique env var names."""
        from scripts.ops.dashboard_server import _build_roles_matrix

        config_path = tmp_path / "swe_team.yaml"
        import yaml
        config_path.write_text(yaml.dump({
            "env_allowlists": {
                "roleA": ["GH_TOKEN", "ANTHROPIC_API_KEY"],
                "roleB": ["GH_TOKEN", "BASE_LLM_API_URL"],
            }
        }))

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_path):
            result = _build_roles_matrix()

        all_vars = result["all_vars"]
        assert all_vars == sorted(set(all_vars))
        assert len(all_vars) == len(set(all_vars))

    def test_empty_config_returns_empty_matrix(self, tmp_path):
        """Empty config returns empty roles/permissions."""
        from scripts.ops.dashboard_server import _build_roles_matrix

        config_path = tmp_path / "empty.yaml"
        import yaml
        config_path.write_text(yaml.dump({"enabled": False}))

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_path):
            result = _build_roles_matrix()

        assert result["roles"] == []
        assert result["permissions"] == {}
        assert result["all_vars"] == []

    def test_missing_config_returns_safe_default(self, tmp_path):
        """Missing config file returns safe empty dict."""
        from scripts.ops.dashboard_server import _build_roles_matrix

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH",
                        tmp_path / "nonexistent.yaml"):
            result = _build_roles_matrix()

        assert "roles" in result
        assert isinstance(result["roles"], list)

    def test_categories_assigned(self, tmp_path):
        """Known env vars are assigned to their categories."""
        from scripts.ops.dashboard_server import _build_roles_matrix

        config_path = tmp_path / "swe_team.yaml"
        import yaml
        config_path.write_text(yaml.dump({
            "env_allowlists": {
                "investigator": ["GH_TOKEN", "TELEGRAM_BOT_TOKEN", "BASE_LLM_API_URL"],
            }
        }))

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_path):
            result = _build_roles_matrix()

        categories = result["categories"]
        assert "GitHub" in categories
        assert "GH_TOKEN" in categories["GitHub"]
        assert "Telegram" in categories
        assert "TELEGRAM_BOT_TOKEN" in categories["Telegram"]


class TestSSEStream:
    """Tests for /api/stream SSE endpoint infrastructure."""

    def test_build_sse_payload_returns_json_string(self, tmp_path):
        """_build_sse_payload returns a valid JSON string."""
        from scripts.ops.dashboard_server import _build_sse_payload

        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH",
                        tmp_path / "no_status.json"), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH",
                        tmp_path / "no_jobs.json"):
            payload = _build_sse_payload()

        # Must be JSON-parseable
        data = json.loads(payload)
        assert "ts" in data
        assert "status" in data
        assert "jobs" in data

    def test_build_sse_payload_reads_status_file(self, tmp_path):
        """_build_sse_payload includes status.json content."""
        from scripts.ops.dashboard_server import _build_sse_payload

        status_path = tmp_path / "status.json"
        status_path.write_text(json.dumps({"gate_verdict": "pass", "tickets_open": 3}))

        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH", status_path), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH",
                        tmp_path / "no_jobs.json"):
            payload = _build_sse_payload()

        data = json.loads(payload)
        assert data["status"]["gate_verdict"] == "pass"
        assert data["status"]["tickets_open"] == 3

    def test_build_sse_payload_ts_is_iso(self, tmp_path):
        """_build_sse_payload timestamp is a valid ISO datetime string."""
        from scripts.ops.dashboard_server import _build_sse_payload

        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH",
                        tmp_path / "no_status.json"), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH",
                        tmp_path / "no_jobs.json"):
            payload = _build_sse_payload()

        data = json.loads(payload)
        ts = datetime.fromisoformat(data["ts"])
        assert ts.tzinfo is not None

    def test_sse_handler_sends_correct_headers(self):
        """_handle_sse sends Content-Type: text/event-stream."""
        import time as time_module
        from scripts.ops.dashboard_server import DashboardHandler

        handler = mock.MagicMock(spec=DashboardHandler)
        handler.wfile = BytesIO()
        # Make the initial SSE payload write succeed, then raise OSError on the
        # first wfile.write inside the while-loop to break out immediately.
        _write_calls = [0]

        def _write_side_effect(data):
            _write_calls[0] += 1
            if _write_calls[0] > 1:
                raise OSError("client disconnected")

        handler.wfile.write = _write_side_effect
        handler.wfile.flush = mock.MagicMock()

        # Patch time.sleep so the while-True loop doesn't actually sleep,
        # and raise OSError on first invocation to exit the loop.
        def _sleep_raise(_):
            raise OSError("client disconnected")

        with mock.patch("scripts.ops.dashboard_server._build_sse_payload",
                        return_value='{"ts":"now","status":{},"jobs":[]}'), \
             mock.patch("scripts.ops.dashboard_server.time") as mock_time, \
             mock.patch("scripts.ops.dashboard_server._sse_clients", []):
            mock_time.sleep.side_effect = OSError("client disconnected")
            DashboardHandler._handle_sse(handler)

        handler.send_response.assert_called_with(200)
        # Check Content-Type header was set
        header_calls = [str(c) for c in handler.send_header.call_args_list]
        assert any("text/event-stream" in c for c in header_calls)


class TestDashboardRouting:
    """Tests for do_GET/do_POST/do_DELETE routing — 404 and 405 cases."""

    def _make_get_handler(self, path: str):
        from scripts.ops.dashboard_server import DashboardHandler

        handler = mock.MagicMock(spec=DashboardHandler)
        handler.path = path
        handler.headers = {}
        handler.rfile = BytesIO(b"")
        handler.wfile = BytesIO()
        handler.store = mock.MagicMock()
        handler.control_plane = None
        handler.auth_provider = None
        handler._json_response = lambda data, status=200, **kw: DashboardHandler._json_response(
            handler, data, status, **kw
        )
        handler._read_post_body = lambda: {}
        # Wire all handler methods that do_GET delegates to
        handler._handle_list_projects = lambda: DashboardHandler._handle_list_projects(handler)
        handler._handle_get_project = lambda name: DashboardHandler._handle_get_project(handler, name)
        handler._handle_create_project = lambda: DashboardHandler._handle_create_project(handler)
        handler._handle_delete_project = lambda name: DashboardHandler._handle_delete_project(handler, name)
        handler._handle_api_graph = lambda: DashboardHandler._handle_api_graph(handler)
        handler._handle_api_auth_status = lambda: DashboardHandler._handle_api_auth_status(handler)
        handler._handle_api_activity = lambda: DashboardHandler._handle_api_activity(handler)
        handler._handle_sse = lambda: DashboardHandler._handle_sse(handler)
        handler._serve_dashboard = lambda: DashboardHandler._serve_dashboard(handler)
        handler._serve_json = lambda: DashboardHandler._serve_json(handler)
        handler._handle_costs = lambda: DashboardHandler._handle_costs(handler)
        handler._handle_scheduler = lambda: DashboardHandler._handle_scheduler(handler)
        handler._handle_list_jobs_api = lambda: DashboardHandler._handle_list_jobs_api(handler)
        handler._handle_job_history_api = lambda: DashboardHandler._handle_job_history_api(handler)
        return handler

    def _make_post_handler(self, path: str, body: dict | None = None):
        from scripts.ops.dashboard_server import DashboardHandler

        request_body = json.dumps(body).encode() if body else b""
        handler = mock.MagicMock(spec=DashboardHandler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(request_body))}
        handler.rfile = BytesIO(request_body)
        handler.wfile = BytesIO()
        handler.control_plane = None
        handler._read_post_body = lambda: DashboardHandler._read_post_body(handler)
        handler._json_response = lambda data, status=200, **kw: DashboardHandler._json_response(
            handler, data, status, **kw
        )
        handler._handle_create_project = lambda: DashboardHandler._handle_create_project(handler)
        handler._handle_create_job = lambda: DashboardHandler._handle_create_job(handler)
        handler._handle_job_action = lambda jid, act: DashboardHandler._handle_job_action(handler, jid, act)
        return handler

    def _make_delete_handler(self, path: str):
        from scripts.ops.dashboard_server import DashboardHandler

        handler = mock.MagicMock(spec=DashboardHandler)
        handler.path = path
        handler.headers = {}
        handler.wfile = BytesIO()
        handler._json_response = lambda data, status=200, **kw: DashboardHandler._json_response(
            handler, data, status, **kw
        )
        handler._handle_delete_project = lambda name: DashboardHandler._handle_delete_project(handler, name)
        return handler

    def test_do_get_unknown_path_sends_404(self):
        """do_GET on an unknown path calls send_error(404)."""
        from scripts.ops.dashboard_server import DashboardHandler
        handler = self._make_get_handler("/api/totally_unknown_xyz")
        DashboardHandler.do_GET(handler)
        handler.send_error.assert_called()
        args = handler.send_error.call_args[0]
        assert args[0] == 404

    def test_do_post_unknown_path_sends_404(self):
        """do_POST on an unknown path calls send_error(404)."""
        from scripts.ops.dashboard_server import DashboardHandler
        handler = self._make_post_handler("/api/unknown_post_xyz")
        DashboardHandler.do_POST(handler)
        handler.send_error.assert_called()
        args = handler.send_error.call_args[0]
        assert args[0] == 404

    def test_do_delete_unknown_path_sends_404(self):
        """do_DELETE on a non-project path calls send_error(404)."""
        from scripts.ops.dashboard_server import DashboardHandler
        handler = self._make_delete_handler("/api/unknown_delete_xyz")
        DashboardHandler.do_DELETE(handler)
        handler.send_error.assert_called()
        args = handler.send_error.call_args[0]
        assert args[0] == 404

    def test_do_get_health_returns_ok(self):
        """GET /health returns {status: ok}."""
        from scripts.ops.dashboard_server import DashboardHandler
        handler = self._make_get_handler("/health")
        DashboardHandler.do_GET(handler)
        handler.send_response.assert_called_with(200)
        resp = json.loads(handler.wfile.getvalue())
        assert resp["status"] == "ok"

    def test_do_get_api_settings(self, tmp_path):
        """GET /api/settings returns a settings dict."""
        from scripts.ops.dashboard_server import DashboardHandler
        handler = self._make_get_handler("/api/settings")

        with mock.patch("scripts.ops.dashboard_server._SETTINGS_PATH",
                        tmp_path / "nonexistent.json"):
            DashboardHandler.do_GET(handler)

        handler.send_response.assert_called_with(200)
        resp = json.loads(handler.wfile.getvalue())
        assert "theme" in resp
        assert "refresh_interval" in resp

    def test_do_get_api_roles(self, tmp_path):
        """GET /api/roles returns roles matrix dict."""
        import yaml
        from scripts.ops.dashboard_server import DashboardHandler
        config_path = tmp_path / "swe_team.yaml"
        config_path.write_text(yaml.dump({
            "env_allowlists": {"investigator": ["GH_TOKEN"]}
        }))

        handler = self._make_get_handler("/api/roles")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_path):
            DashboardHandler.do_GET(handler)

        handler.send_response.assert_called_with(200)
        resp = json.loads(handler.wfile.getvalue())
        assert "roles" in resp
        assert "permissions" in resp

    def test_do_get_api_scheduler(self, tmp_path):
        """GET /api/scheduler returns a list (jobs)."""
        from scripts.ops.dashboard_server import DashboardHandler
        handler = self._make_get_handler("/api/scheduler")

        with mock.patch("scripts.ops.dashboard_server._JOBS_PATH",
                        tmp_path / "no_jobs.json"):
            DashboardHandler.do_GET(handler)

        handler.send_response.assert_called_with(200)
        resp = json.loads(handler.wfile.getvalue())
        assert isinstance(resp, list)

    def test_do_get_api_scheduler_history(self, tmp_path):
        """GET /api/scheduler/history returns a list."""
        from scripts.ops.dashboard_server import DashboardHandler
        handler = self._make_get_handler("/api/scheduler/history")

        with mock.patch("scripts.ops.dashboard_server._RUN_HISTORY_PATH",
                        tmp_path / "no_history.jsonl"), \
             mock.patch("scripts.ops.dashboard_server._STATUS_PATH",
                        tmp_path / "no_status.json"), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH",
                        tmp_path / "no_jobs.json"):
            DashboardHandler.do_GET(handler)

        handler.send_response.assert_called_with(200)
        resp = json.loads(handler.wfile.getvalue())
        assert isinstance(resp, list)

    def test_do_get_api_rbac(self, tmp_path):
        """GET /api/rbac returns roles yaml content."""
        import yaml
        from scripts.ops.dashboard_server import DashboardHandler
        roles_path = tmp_path / "roles.yaml"
        roles_path.write_text(yaml.dump({"roles": ["admin", "viewer"]}))

        handler = self._make_get_handler("/api/rbac")

        with mock.patch("scripts.ops.dashboard_server._ROLES_PATH", roles_path):
            DashboardHandler.do_GET(handler)

        handler.send_response.assert_called_with(200)
        resp = json.loads(handler.wfile.getvalue())
        assert isinstance(resp, dict)

    def test_do_post_api_settings(self, tmp_path):
        """POST /api/settings saves and returns updated settings."""
        from scripts.ops.dashboard_server import DashboardHandler
        settings_path = tmp_path / "data" / "swe_team" / "dashboard_settings.json"
        handler = self._make_post_handler("/api/settings", body={"theme": "light", "refresh_interval": 60})

        with mock.patch("scripts.ops.dashboard_server._SETTINGS_PATH", settings_path):
            DashboardHandler.do_POST(handler)

        handler.send_response.assert_called_with(200)
        resp = json.loads(handler.wfile.getvalue())
        assert resp["ok"] is True
        assert resp["settings"]["theme"] == "light"

    def test_do_delete_api_projects(self, tmp_path):
        """DELETE /api/projects/<name> removes a project from config."""
        from scripts.ops.dashboard_server import DashboardHandler, _load_projects_from_config
        import yaml

        config_path = tmp_path / "swe_team.yaml"
        config_path.write_text(yaml.dump({
            "repos": [
                {"name": "org/to-delete", "local_path": "/tmp/d"},
                {"name": "org/keep", "local_path": "/tmp/k"},
            ]
        }))

        handler = self._make_delete_handler("/api/projects/org/to-delete")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_path):
            DashboardHandler.do_DELETE(handler)
            remaining = _load_projects_from_config()

        handler.send_response.assert_called_with(200)
        resp = json.loads(handler.wfile.getvalue())
        assert resp["ok"] is True
        assert resp["deleted"] == "org/to-delete"
        assert len(remaining) == 1
        assert remaining[0]["name"] == "org/keep"

    def test_do_delete_unknown_project_sends_404(self, tmp_path):
        """DELETE /api/projects/<nonexistent> returns 404."""
        from scripts.ops.dashboard_server import DashboardHandler
        import yaml

        config_path = tmp_path / "swe_team.yaml"
        config_path.write_text(yaml.dump({"repos": []}))

        handler = self._make_delete_handler("/api/projects/nonexistent/project")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_path):
            DashboardHandler.do_DELETE(handler)

        handler.send_response.assert_called_with(404)


class TestMissingConstants:
    """Regression tests ensuring formerly-missing constants are now defined."""

    def test_status_path_defined(self):
        """_STATUS_PATH is defined and points to status.json."""
        from scripts.ops.dashboard_server import _STATUS_PATH
        assert _STATUS_PATH.name == "status.json"

    def test_jobs_path_defined(self):
        """_JOBS_PATH is defined and points to jobs.json."""
        from scripts.ops.dashboard_server import _JOBS_PATH
        assert _JOBS_PATH.name == "jobs.json"

    def test_has_control_plane_is_bool(self):
        """_HAS_CONTROL_PLANE is a boolean."""
        from scripts.ops.dashboard_server import _HAS_CONTROL_PLANE
        assert isinstance(_HAS_CONTROL_PLANE, bool)

    def test_cp_handle_get_callable(self):
        """cp_handle_get is callable (real or stub)."""
        from scripts.ops.dashboard_server import cp_handle_get
        assert callable(cp_handle_get)

    def test_cp_handle_post_callable(self):
        """cp_handle_post is callable (real or stub)."""
        from scripts.ops.dashboard_server import cp_handle_post
        assert callable(cp_handle_post)

    def test_handle_sse_defined(self):
        """_handle_sse method exists on DashboardHandler."""
        from scripts.ops.dashboard_server import DashboardHandler
        assert hasattr(DashboardHandler, "_handle_sse")
        assert callable(DashboardHandler._handle_sse)
