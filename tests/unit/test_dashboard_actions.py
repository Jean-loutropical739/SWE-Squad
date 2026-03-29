"""
Tests for dashboard action API endpoints (issue #284).

Covers:
  - GET /api/tickets/<id> — single ticket detail
  - POST /api/tickets/<id>/assign — assign ticket
  - POST /api/tickets/<id>/investigate — trigger investigation (queued)
  - POST /api/tickets/<id>/develop — trigger developer agent (queued)
  - PATCH /api/tickets/<id>/status — change ticket status
  - PATCH /api/tickets/<id>/severity — change ticket severity
  - POST /api/tickets/<id>/comment — add comment
  - POST /api/tickets/<id>/label — update labels
  - POST /api/pipeline/trigger — trigger pipeline cycle (queued)
  - GET /api/tickets/export — CSV export
  - SSE broadcast helper
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Project bootstrap ─────────────────────────────────────────────────────────
logging.logAsyncioTasks = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.ticket_store import TicketStore


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_store(tmp_path):
    """Provide a TicketStore backed by a temp directory."""
    path = tmp_path / "tickets.json"
    store = TicketStore(path=str(path))
    return store


@pytest.fixture
def sample_ticket():
    """Provide a sample SWETicket."""
    return SWETicket(
        ticket_id="test-abc123",
        title="Test ticket for dashboard actions",
        description="A ticket used for testing action API endpoints.",
        severity=TicketSeverity.HIGH,
        status=TicketStatus.TRIAGED,
        source_module="test_module",
        assigned_to=None,
        metadata={"github_issue_number": 42, "fingerprint": "fp-001"},
    )


@pytest.fixture
def store_with_ticket(tmp_store, sample_ticket):
    """Store with a pre-loaded ticket."""
    tmp_store.add(sample_ticket)
    return tmp_store


# ══════════════════════════════════════════════════════════════════════════════
# Helper: Mock handler that exercises route-matching logic
# ══════════════════════════════════════════════════════════════════════════════

def _make_handler(store):
    """Create a DashboardHandler instance configured for unit tests."""
    from scripts.ops.dashboard_server import DashboardHandler

    handler = MagicMock(spec=DashboardHandler)
    handler.store = store
    handler.auth_provider = None
    handler.headers = {"Content-Length": "0"}

    # Wire up real methods
    handler._read_post_body = DashboardHandler._read_post_body.__get__(handler)
    handler._json_response = DashboardHandler._json_response.__get__(handler)
    handler._handle_get_ticket = DashboardHandler._handle_get_ticket.__get__(handler)
    handler._handle_ticket_assign = DashboardHandler._handle_ticket_assign.__get__(handler)
    handler._handle_ticket_status = DashboardHandler._handle_ticket_status.__get__(handler)
    handler._handle_ticket_severity = DashboardHandler._handle_ticket_severity.__get__(handler)
    handler._handle_ticket_comment = DashboardHandler._handle_ticket_comment.__get__(handler)
    handler._handle_ticket_label = DashboardHandler._handle_ticket_label.__get__(handler)
    handler._handle_ticket_investigate = DashboardHandler._handle_ticket_investigate.__get__(handler)
    handler._handle_ticket_develop = DashboardHandler._handle_ticket_develop.__get__(handler)
    handler._handle_pipeline_trigger = DashboardHandler._handle_pipeline_trigger.__get__(handler)
    handler._handle_tickets_export = DashboardHandler._handle_tickets_export.__get__(handler)
    handler._gh_comment_async = DashboardHandler._gh_comment_async.__get__(handler)

    return handler


def _set_body(handler, body_dict):
    """Set the POST body for a mock handler."""
    raw = json.dumps(body_dict).encode()
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)


def _capture_json(handler):
    """Extract the JSON body from the last _json_response call or wfile.write."""
    # Check _json_response calls
    for call_args in handler._json_response.call_args_list:
        return call_args[0][0]
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Tests: GET /api/tickets/<id>
# ══════════════════════════════════════════════════════════════════════════════

class TestGetTicket:
    def test_get_existing_ticket(self, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        # Call the real method but capture response via mock
        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_get_ticket("test-abc123")
        assert len(response_data) == 1
        data, status = response_data[0]
        assert status == 200
        assert data["ticket_id"] == "test-abc123"
        assert data["title"] == "Test ticket for dashboard actions"
        assert data["severity"] == "high"

    def test_get_nonexistent_ticket(self, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_get_ticket("nonexistent")
        assert len(response_data) == 1
        data, status = response_data[0]
        assert status == 404
        assert "error" in data


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/tickets/<id>/assign
# ══════════════════════════════════════════════════════════════════════════════

class TestTicketAssign:
    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server.os.environ", {"SWE_GITHUB_REPO": ""})
    def test_assign_ticket(self, mock_sse, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"assignee": "investigator-agent"})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_assign("test-abc123")
        assert len(response_data) == 1
        data, status = response_data[0]
        assert status == 200
        assert data["assignee"] == "investigator-agent"

        ticket = store_with_ticket.get("test-abc123")
        assert ticket.assigned_to == "investigator-agent"

        mock_sse.assert_called_once()
        sse_payload = mock_sse.call_args[0][1]
        assert sse_payload["event"] == "ticket_assigned"

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_assign_missing_assignee(self, mock_sse, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_assign("test-abc123")
        data, status = response_data[0]
        assert status == 400
        assert "error" in data

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_assign_nonexistent_ticket(self, mock_sse, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"assignee": "agent"})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_assign("nonexistent")
        data, status = response_data[0]
        assert status == 404


# ══════════════════════════════════════════════════════════════════════════════
# Tests: PATCH /api/tickets/<id>/status
# ══════════════════════════════════════════════════════════════════════════════

class TestTicketStatus:
    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server.os.environ", {"SWE_GITHUB_REPO": ""})
    def test_change_status(self, mock_sse, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"status": "investigating"})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_status("test-abc123")
        data, status = response_data[0]
        assert status == 200
        assert data["new_status"] == "investigating"

        ticket = store_with_ticket.get("test-abc123")
        assert ticket.status == TicketStatus.INVESTIGATING

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_invalid_status(self, mock_sse, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"status": "bogus"})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_status("test-abc123")
        data, status = response_data[0]
        assert status == 400
        assert "error" in data

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_status_missing(self, mock_sse, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_status("test-abc123")
        data, status = response_data[0]
        assert status == 400

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server.os.environ", {"SWE_GITHUB_REPO": ""})
    def test_resolve_with_bypass_note(self, mock_sse, store_with_ticket):
        """Resolving a HIGH ticket requires bypass note or investigation."""
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"status": "resolved", "resolution_note": "manual_override"})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_status("test-abc123")
        data, status = response_data[0]
        assert status == 200
        assert data["new_status"] == "resolved"

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_resolve_blocked_without_bypass(self, mock_sse, store_with_ticket):
        """Resolving without bypass should fail for HIGH ticket without investigation."""
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"status": "resolved"})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_status("test-abc123")
        data, status = response_data[0]
        assert status == 422  # Resolution blocked


# ══════════════════════════════════════════════════════════════════════════════
# Tests: PATCH /api/tickets/<id>/severity
# ══════════════════════════════════════════════════════════════════════════════

class TestTicketSeverity:
    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_change_severity(self, mock_sse, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"severity": "critical"})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_severity("test-abc123")
        data, status = response_data[0]
        assert status == 200
        assert data["new_severity"] == "critical"

        ticket = store_with_ticket.get("test-abc123")
        assert ticket.severity == TicketSeverity.CRITICAL

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_invalid_severity(self, mock_sse, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"severity": "apocalyptic"})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_severity("test-abc123")
        data, status = response_data[0]
        assert status == 400


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/tickets/<id>/comment
# ══════════════════════════════════════════════════════════════════════════════

class TestTicketComment:
    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server.os.environ", {"SWE_GITHUB_REPO": ""})
    def test_add_comment(self, mock_sse, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"comment": "Investigating the root cause now."})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_comment("test-abc123")
        data, status = response_data[0]
        assert status == 200

        ticket = store_with_ticket.get("test-abc123")
        assert len(ticket.metadata["comments"]) == 1
        assert ticket.metadata["comments"][0]["text"] == "Investigating the root cause now."
        assert ticket.metadata["comments"][0]["source"] == "dashboard"

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_empty_comment_rejected(self, mock_sse, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"comment": ""})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_comment("test-abc123")
        data, status = response_data[0]
        assert status == 400


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/tickets/<id>/label
# ══════════════════════════════════════════════════════════════════════════════

class TestTicketLabel:
    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server.os.environ", {"SWE_GITHUB_REPO": ""})
    def test_add_and_remove_labels(self, mock_sse, store_with_ticket):
        # First add a label
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"add": ["bug", "regression"], "remove": []})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_label("test-abc123")
        data, status = response_data[0]
        assert status == 200
        assert "bug" in data["labels"]
        assert "regression" in data["labels"]

        # Now remove one
        handler2 = _make_handler(store_with_ticket)
        _set_body(handler2, {"add": [], "remove": ["bug"]})

        response_data2: list = []
        def capture2(data, status=200):
            response_data2.append((data, status))
        handler2._json_response = capture2

        handler2._handle_ticket_label("test-abc123")
        data2, status2 = response_data2[0]
        assert status2 == 200
        assert "bug" not in data2["labels"]
        assert "regression" in data2["labels"]


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/tickets/<id>/investigate and /develop (background)
# ══════════════════════════════════════════════════════════════════════════════

class TestTicketInvestigate:
    def test_investigate_queues_and_returns(self, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"model": "sonnet"})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler._handle_ticket_investigate("test-abc123")

        data, status = response_data[0]
        assert status == 200
        assert data["status"] == "queued"
        assert data["action"] == "investigate"

    def test_investigate_nonexistent(self, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_ticket_investigate("nonexistent")
        data, status = response_data[0]
        assert status == 404


class TestTicketDevelop:
    def test_develop_queues_and_returns(self, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        _set_body(handler, {"model": "sonnet"})

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler._handle_ticket_develop("test-abc123")

        data, status = response_data[0]
        assert status == 200
        assert data["status"] == "queued"
        assert data["action"] == "develop"


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/pipeline/trigger
# ══════════════════════════════════════════════════════════════════════════════

class TestPipelineTrigger:
    def test_pipeline_trigger(self, store_with_ticket):
        handler = _make_handler(store_with_ticket)

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            handler._handle_pipeline_trigger()

        data, status = response_data[0]
        assert status == 200
        assert data["status"] == "triggered"


# ══════════════════════════════════════════════════════════════════════════════
# Tests: GET /api/tickets/export
# ══════════════════════════════════════════════════════════════════════════════

class TestTicketsExport:
    def test_csv_export(self, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler._handle_tickets_export({"format": ["csv"]})

        body = handler.wfile.getvalue()
        text = body.decode("utf-8")
        assert "ticket_id" in text  # header row
        assert "test-abc123" in text

    def test_json_export(self, store_with_ticket):
        handler = _make_handler(store_with_ticket)

        response_data: list = []
        def capture(data, status=200):
            response_data.append((data, status))
        handler._json_response = capture

        handler._handle_tickets_export({"format": ["json"]})
        data, status = response_data[0]
        assert status == 200
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["ticket_id"] == "test-abc123"


# ══════════════════════════════════════════════════════════════════════════════
# Tests: SSE broadcast helper
# ══════════════════════════════════════════════════════════════════════════════

class TestSSEBroadcast:
    def test_broadcast_sends_to_clients(self):
        from scripts.ops.dashboard_server import _broadcast_sse_event, _sse_clients, _sse_lock

        mock_wfile = io.BytesIO()
        with _sse_lock:
            _sse_clients.append(mock_wfile)
        try:
            _broadcast_sse_event("test_event", {"foo": "bar"})
            output = mock_wfile.getvalue().decode("utf-8")
            assert "event: test_event" in output
            assert '"foo": "bar"' in output
        finally:
            with _sse_lock:
                if mock_wfile in _sse_clients:
                    _sse_clients.remove(mock_wfile)

    def test_broadcast_removes_dead_client(self):
        from scripts.ops.dashboard_server import _broadcast_sse_event, _sse_clients, _sse_lock

        class DeadWfile:
            def write(self, data):
                raise BrokenPipeError("dead")
            def flush(self):
                raise BrokenPipeError("dead")

        dead = DeadWfile()
        with _sse_lock:
            _sse_clients.append(dead)

        _broadcast_sse_event("cleanup", {"x": 1})

        with _sse_lock:
            assert dead not in _sse_clients
