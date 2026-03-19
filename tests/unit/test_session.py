"""Tests for src/swe_team/session.py"""
from datetime import datetime, timezone

import pytest

from src.swe_team.session import make_session_tag, session_header


class TestMakeSessionTag:
    def test_tag_with_issue_number(self):
        tag = make_session_tag(issue_number=42)
        assert "SWE-SQUAD-ISSUE#42" in tag
        assert "[trace:" in tag

    def test_tag_with_issue_number_zero(self):
        """issue_number=0 should still be included (not falsy check)."""
        tag = make_session_tag(issue_number=0)
        assert "SWE-SQUAD-ISSUE#0" in tag

    def test_tag_with_ticket_id(self):
        tag = make_session_tag(ticket_id="abc123def456xyz")
        assert "SWE-SQUAD-TICKET-abc123def456" in tag
        assert "[trace:" in tag

    def test_tag_with_cycle(self):
        tag = make_session_tag(cycle=True)
        assert "SWE-SQUAD-CYCLE-" in tag
        assert "[trace:" in tag

    def test_tag_with_none_falls_back_to_session(self):
        tag = make_session_tag()
        assert "SWE-SQUAD-SESSION-" in tag
        assert "[trace:" in tag

    def test_priority_issue_over_ticket(self):
        tag = make_session_tag(issue_number=7, ticket_id="abc123")
        assert "SWE-SQUAD-ISSUE#7" in tag
        assert "TICKET" not in tag

    def test_priority_ticket_over_cycle(self):
        tag = make_session_tag(ticket_id="abc123", cycle=True)
        assert "SWE-SQUAD-TICKET-" in tag
        assert "CYCLE" not in tag

    def test_trace_length_is_12(self):
        tag = make_session_tag(issue_number=1)
        # extract trace: between "[trace:" and "]"
        start = tag.index("[trace:") + len("[trace:")
        end = tag.index("]", start)
        trace = tag[start:end]
        assert len(trace) == 12


class TestSessionHeader:
    def test_header_format(self):
        tag = "SWE-SQUAD-ISSUE#5 [trace:abc123def456]"
        header = session_header(tag)
        assert "**Session:**" in header
        assert tag in header
        assert "**Started:**" in header
        assert "**Agent:** SWE-Squad (Claude Code)" in header

    def test_header_uses_provided_started_at(self):
        fixed_dt = datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        tag = "SWE-SQUAD-CYCLE-20250115T1030"
        header = session_header(tag, started_at=fixed_dt)
        assert "2025-01-15 10:30 UTC" in header

    def test_header_defaults_to_now_when_no_started_at(self):
        tag = "SWE-SQUAD-CYCLE-test"
        header = session_header(tag)
        assert "**Started:**" in header
        # Just verify it has a date-like string in UTC format
        assert "UTC" in header
