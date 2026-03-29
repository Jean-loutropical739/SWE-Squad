"""Tests for Rich terminal output in swe_cli.py (issue #126).

Covers:
  - Status command renders with Rich (panel output)
  - Tickets command renders colour-coded table
  - --json flag bypasses Rich and returns raw JSON
  - Graceful fallback when rich is not installed
  - Project list renders with Rich
  - Summary renders with Rich
  - cli_rich module helpers
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Project bootstrap ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_ticket(
    ticket_id: str = "T-001",
    title: str = "Test ticket",
    severity: TicketSeverity = TicketSeverity.HIGH,
    status: TicketStatus = TicketStatus.OPEN,
    assigned_to: str = "investigator",
) -> SWETicket:
    return SWETicket(
        title=title,
        description="test description",
        severity=severity,
        status=status,
        ticket_id=ticket_id,
        assigned_to=assigned_to,
    )


@pytest.fixture
def mock_ticket_store():
    """Return a mock TicketStore with sample tickets."""
    store = MagicMock()
    tickets = [
        _make_ticket("T-001", "Critical DB failure", TicketSeverity.CRITICAL, TicketStatus.OPEN),
        _make_ticket("T-002", "High memory usage", TicketSeverity.HIGH, TicketStatus.INVESTIGATING),
        _make_ticket("T-003", "Minor CSS issue", TicketSeverity.LOW, TicketStatus.RESOLVED),
    ]
    store.list_all.return_value = tickets
    store.list_open.return_value = [t for t in tickets if t.status not in (TicketStatus.RESOLVED, TicketStatus.CLOSED)]
    store.list_recently_resolved.return_value = [tickets[2]]
    return store


@pytest.fixture
def empty_status_path(tmp_path):
    """Create a temp dir and patch STATUS_PATH to a non-existent file."""
    return tmp_path / "status.json"


# ── Tests: Rich module imports ────────────────────────────────────────────────


class TestCliRichModule:
    """Test the cli_rich helper module itself."""

    def test_has_rich_flag(self):
        from scripts.ops.cli_rich import HAS_RICH
        # rich is installed in this env, so should be True
        assert HAS_RICH is True

    def test_console_exists(self):
        from scripts.ops.cli_rich import console
        assert console is not None

    def test_severity_styles_defined(self):
        from scripts.ops.cli_rich import _SEV_STYLE
        assert "critical" in _SEV_STYLE
        assert "high" in _SEV_STYLE
        assert "medium" in _SEV_STYLE
        assert "low" in _SEV_STYLE

    def test_gate_styles_defined(self):
        from scripts.ops.cli_rich import _GATE_STYLE
        assert "PASS" in _GATE_STYLE
        assert "WARN" in _GATE_STYLE
        assert "BLOCK" in _GATE_STYLE


# ── Tests: Status command ─────────────────────────────────────────────────────


class TestStatusRich:
    """Test cmd_status with Rich output."""

    def test_status_json_bypasses_rich(self, mock_ticket_store, capsys):
        """--json flag must produce raw JSON, never Rich markup."""
        from scripts.ops.swe_cli import build_parser, cmd_status

        parser = build_parser()
        args = parser.parse_args(["status", "--json"])

        with patch("scripts.ops.swe_cli._load_status", return_value=None), \
             patch("scripts.ops.swe_cli._get_ticket_store", return_value=mock_ticket_store):
            rc = cmd_status(args)

        assert rc == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert "ticket_counts" in data

    def test_status_rich_renders(self, mock_ticket_store):
        """Status command with Rich should not raise."""
        from scripts.ops.swe_cli import build_parser, cmd_status

        parser = build_parser()
        args = parser.parse_args(["status"])

        status_data = {
            "last_cycle": "2026-03-21T10:00:00",
            "gate_verdict": "PASS",
            "next_cycle": "2026-03-21T11:00:00",
            "tickets_open": 2,
            "tickets_investigating": 1,
        }

        with patch("scripts.ops.swe_cli._load_status", return_value=status_data), \
             patch("scripts.ops.swe_cli._get_ticket_store", return_value=mock_ticket_store):
            rc = cmd_status(args)
        assert rc == 0

    def test_status_no_status_file(self, mock_ticket_store):
        """Status renders even when status.json is missing."""
        from scripts.ops.swe_cli import build_parser, cmd_status

        parser = build_parser()
        args = parser.parse_args(["status"])

        with patch("scripts.ops.swe_cli._load_status", return_value=None), \
             patch("scripts.ops.swe_cli._get_ticket_store", return_value=mock_ticket_store):
            rc = cmd_status(args)
        assert rc == 0


# ── Tests: Tickets command ────────────────────────────────────────────────────


class TestTicketsRich:
    """Test cmd_tickets with Rich output."""

    def test_tickets_json_bypasses_rich(self, mock_ticket_store, capsys):
        """--json produces raw JSON."""
        from scripts.ops.swe_cli import build_parser, cmd_tickets

        parser = build_parser()
        args = parser.parse_args(["tickets", "--json"])

        with patch("scripts.ops.swe_cli._get_ticket_store", return_value=mock_ticket_store):
            rc = cmd_tickets(args)

        assert rc == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert isinstance(data, list)

    def test_tickets_rich_renders(self, mock_ticket_store):
        """Tickets command with Rich should not raise."""
        from scripts.ops.swe_cli import build_parser, cmd_tickets

        parser = build_parser()
        args = parser.parse_args(["tickets"])

        with patch("scripts.ops.swe_cli._get_ticket_store", return_value=mock_ticket_store):
            rc = cmd_tickets(args)
        assert rc == 0

    def test_tickets_empty(self, capsys):
        """Empty ticket store shows message."""
        from scripts.ops.swe_cli import build_parser, cmd_tickets

        empty_store = MagicMock()
        empty_store.list_all.return_value = []

        parser = build_parser()
        args = parser.parse_args(["tickets"])

        with patch("scripts.ops.swe_cli._get_ticket_store", return_value=empty_store):
            rc = cmd_tickets(args)

        assert rc == 0
        output = capsys.readouterr().out
        assert "No tickets found" in output

    def test_tickets_severity_filter(self, mock_ticket_store):
        """Severity filter works with Rich output."""
        from scripts.ops.swe_cli import build_parser, cmd_tickets

        parser = build_parser()
        args = parser.parse_args(["tickets", "--severity", "critical"])

        with patch("scripts.ops.swe_cli._get_ticket_store", return_value=mock_ticket_store):
            rc = cmd_tickets(args)
        assert rc == 0


# ── Tests: Summary command ────────────────────────────────────────────────────


class TestSummaryRich:
    """Test cmd_summary with Rich output."""

    def test_summary_json_bypasses_rich(self, mock_ticket_store, capsys):
        from scripts.ops.swe_cli import build_parser, cmd_summary

        parser = build_parser()
        args = parser.parse_args(["summary", "--json"])

        with patch("scripts.ops.swe_cli._load_status", return_value=None), \
             patch("scripts.ops.swe_cli._get_ticket_store", return_value=mock_ticket_store):
            rc = cmd_summary(args)

        assert rc == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert "open_tickets" in data

    def test_summary_rich_renders(self, mock_ticket_store):
        from scripts.ops.swe_cli import build_parser, cmd_summary

        parser = build_parser()
        args = parser.parse_args(["summary"])

        with patch("scripts.ops.swe_cli._load_status", return_value={"gate_verdict": "PASS", "last_cycle": "now"}), \
             patch("scripts.ops.swe_cli._get_ticket_store", return_value=mock_ticket_store):
            rc = cmd_summary(args)
        assert rc == 0


# ── Tests: Project list ───────────────────────────────────────────────────────


class TestProjectListRich:
    """Test project list with Rich output."""

    def test_project_list_rich_renders(self):
        from scripts.ops.swe_cli import build_parser, cmd_project

        parser = build_parser()
        args = parser.parse_args(["project", "list"])

        repos = [
            {"name": "test/repo", "local_path": "/tmp/repo", "priority": "high"},
            {"name": "test/repo2", "local_path": "/tmp/repo2", "priority": "low", "monitor_only": True},
        ]

        with patch("scripts.ops.swe_cli._load_config_yaml", return_value={"repos": repos}):
            rc = cmd_project(args)
        assert rc == 0

    def test_project_list_json(self, capsys):
        from scripts.ops.swe_cli import build_parser, cmd_project

        parser = build_parser()
        args = parser.parse_args(["project", "list", "--json"])

        repos = [{"name": "test/repo", "local_path": "/tmp/repo"}]

        with patch("scripts.ops.swe_cli._load_config_yaml", return_value={"repos": repos}):
            rc = cmd_project(args)

        assert rc == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert isinstance(data, list)


# ── Tests: Graceful fallback ─────────────────────────────────────────────────


class TestRichFallback:
    """Test that CLI works when rich is not installed."""

    def test_status_without_rich(self, mock_ticket_store, capsys):
        """When HAS_RICH is False, falls back to plain text."""
        from scripts.ops.swe_cli import build_parser, cmd_status

        parser = build_parser()
        args = parser.parse_args(["status"])

        with patch("scripts.ops.swe_cli.HAS_RICH", False), \
             patch("scripts.ops.swe_cli._load_status", return_value=None), \
             patch("scripts.ops.swe_cli._get_ticket_store", return_value=mock_ticket_store):
            rc = cmd_status(args)

        assert rc == 0
        output = capsys.readouterr().out
        assert "SWE Squad Status" in output

    def test_tickets_without_rich(self, mock_ticket_store, capsys):
        """When HAS_RICH is False, renders plain text table."""
        from scripts.ops.swe_cli import build_parser, cmd_tickets

        parser = build_parser()
        args = parser.parse_args(["tickets"])

        with patch("scripts.ops.swe_cli.HAS_RICH", False), \
             patch("scripts.ops.swe_cli._get_ticket_store", return_value=mock_ticket_store):
            rc = cmd_tickets(args)

        assert rc == 0
        output = capsys.readouterr().out
        assert "TICKET_ID" in output
        assert "SEVERITY" in output

    def test_summary_without_rich(self, mock_ticket_store, capsys):
        """When HAS_RICH is False, renders plain text summary."""
        from scripts.ops.swe_cli import build_parser, cmd_summary

        parser = build_parser()
        args = parser.parse_args(["summary"])

        with patch("scripts.ops.swe_cli.HAS_RICH", False), \
             patch("scripts.ops.swe_cli._load_status", return_value=None), \
             patch("scripts.ops.swe_cli._get_ticket_store", return_value=mock_ticket_store):
            rc = cmd_summary(args)

        assert rc == 0
        output = capsys.readouterr().out
        assert "SWE Squad Summary" in output

    def test_project_list_without_rich(self, capsys):
        """When HAS_RICH is False, project list uses plain text."""
        from scripts.ops.swe_cli import build_parser, cmd_project

        parser = build_parser()
        args = parser.parse_args(["project", "list"])

        repos = [{"name": "test/repo", "local_path": "/tmp/repo", "priority": "medium"}]

        with patch("scripts.ops.swe_cli.HAS_RICH", False), \
             patch("scripts.ops.swe_cli._load_config_yaml", return_value={"repos": repos}):
            rc = cmd_project(args)

        assert rc == 0
        output = capsys.readouterr().out
        assert "NAME" in output


# ── Tests: cli_rich renderers don't crash ─────────────────────────────────────


class TestCliRichRenderers:
    """Smoke-test each renderer in cli_rich to confirm no exceptions."""

    def test_render_status_with_status(self):
        from scripts.ops.cli_rich import render_status
        data = {"ticket_counts": {"total": 5, "open": 2, "investigating": 1, "in_development": 0, "resolved": 2}}
        status = {"last_cycle": "2026-03-21", "gate_verdict": "PASS", "next_cycle": "2026-03-22"}
        render_status(data, status)  # should not raise

    def test_render_status_no_status(self):
        from scripts.ops.cli_rich import render_status
        data = {"ticket_counts": {"total": 0, "open": 0, "investigating": 0, "in_development": 0, "resolved": 0}}
        render_status(data, None)  # should not raise

    def test_render_tickets(self):
        from scripts.ops.cli_rich import render_tickets
        tickets = [
            _make_ticket("T-001", "Test critical", TicketSeverity.CRITICAL, TicketStatus.OPEN),
            _make_ticket("T-002", "Test high", TicketSeverity.HIGH, TicketStatus.INVESTIGATING),
        ]
        render_tickets(tickets, lambda t, w: t[:w])

    def test_render_issues(self):
        from scripts.ops.cli_rich import render_issues
        issues = [
            {"number": 1, "title": "Bug", "createdAt": "2026-03-21T00:00:00Z", "labels": [{"name": "bug"}]},
        ]
        render_issues(issues, lambda t, w: t[:w])

    def test_render_repos(self):
        from scripts.ops.cli_rich import render_repos
        repos = [
            {"name": "swe-squad", "visibility": "PUBLIC", "viewerPermission": "ADMIN"},
        ]
        render_repos(repos, lambda t, w: t[:w])

    def test_render_summary(self):
        from scripts.ops.cli_rich import render_summary
        data = {
            "open_tickets": 2,
            "total_tickets": 5,
            "severity_counts": {"critical": 1, "high": 1},
            "status_counts": {"open": 1, "investigating": 1},
            "recent_investigations_24h": 1,
            "recent_fixes_24h": {"total": 1, "success": 1, "fail": 0},
            "gate_verdict": "WARN",
            "last_cycle": "2026-03-21",
        }
        render_summary(data)

    def test_render_project_list(self):
        from scripts.ops.cli_rich import render_project_list
        repos = [
            {"name": "proj-a", "local_path": "/tmp/a", "priority": "high"},
            {"name": "proj-b", "local_path": "/tmp/b", "priority": "low", "monitor_only": True},
        ]
        render_project_list(repos, lambda t, w: t[:w])

    def test_render_costs(self):
        from scripts.ops.cli_rich import render_costs
        summary = {
            "total_cost_usd": 1.2345,
            "daily_spend": 0.5,
            "total_records": 100,
            "by_model": {
                "sonnet": {"calls": 10, "input_tokens": 5000, "output_tokens": 3000, "cost_usd": 0.8},
            },
        }
        render_costs(summary)

    def test_render_costs_empty(self):
        from scripts.ops.cli_rich import render_costs
        summary = {
            "total_cost_usd": 0.0,
            "daily_spend": 0.0,
            "total_records": 0,
            "by_model": {},
        }
        render_costs(summary)
