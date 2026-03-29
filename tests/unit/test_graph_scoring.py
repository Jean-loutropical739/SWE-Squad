"""
Unit tests for src/swe_team/graph_scoring.py — priority_score() and rank_tickets().
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import MagicMock

import pytest

from src.swe_team.graph_scoring import SEVERITY_WEIGHT, priority_score, rank_tickets
from src.swe_team.models import EdgeType, ResolutionCluster, SWETicket, TicketSeverity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ticket(
    severity: TicketSeverity = TicketSeverity.MEDIUM,
    *,
    created_at: str | None = None,
    attempts: List | None = None,
    is_regression: bool = False,
) -> SWETicket:
    t = SWETicket(title="Test ticket", description="desc", severity=severity)
    if created_at is not None:
        t.created_at = created_at
    if attempts is not None:
        t.metadata["attempts"] = attempts
    if is_regression:
        t.metadata["is_regression"] = True
    return t


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_ago(h: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=h)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# SEVERITY_WEIGHT constants
# ---------------------------------------------------------------------------

class TestSeverityWeights:
    def test_critical_weight_is_10(self):
        assert SEVERITY_WEIGHT[TicketSeverity.CRITICAL] == 10.0

    def test_high_weight_is_5(self):
        assert SEVERITY_WEIGHT[TicketSeverity.HIGH] == 5.0

    def test_medium_weight_is_2(self):
        assert SEVERITY_WEIGHT[TicketSeverity.MEDIUM] == 2.0

    def test_low_weight_is_1(self):
        assert SEVERITY_WEIGHT[TicketSeverity.LOW] == 1.0


# ---------------------------------------------------------------------------
# priority_score() — basic severity ordering
# ---------------------------------------------------------------------------

class TestPriorityScoreBasic:
    def test_returns_numeric_score(self):
        t = _ticket(TicketSeverity.MEDIUM)
        score = priority_score(t)
        assert isinstance(score, float)
        assert score > 0

    def test_critical_scores_higher_than_high(self):
        t_crit = _ticket(TicketSeverity.CRITICAL)
        t_high = _ticket(TicketSeverity.HIGH)
        assert priority_score(t_crit) > priority_score(t_high)

    def test_high_scores_higher_than_medium(self):
        t_high = _ticket(TicketSeverity.HIGH)
        t_med = _ticket(TicketSeverity.MEDIUM)
        assert priority_score(t_high) > priority_score(t_med)

    def test_medium_scores_higher_than_low(self):
        t_med = _ticket(TicketSeverity.MEDIUM)
        t_low = _ticket(TicketSeverity.LOW)
        assert priority_score(t_med) > priority_score(t_low)

    def test_severity_ordering_critical_high_medium_low(self):
        scores = [
            priority_score(_ticket(TicketSeverity.CRITICAL)),
            priority_score(_ticket(TicketSeverity.HIGH)),
            priority_score(_ticket(TicketSeverity.MEDIUM)),
            priority_score(_ticket(TicketSeverity.LOW)),
        ]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# priority_score() — age factor
# ---------------------------------------------------------------------------

class TestAgeFactor:
    def test_older_ticket_scores_higher(self):
        t_new = _ticket(created_at=_hours_ago(1))
        t_old = _ticket(created_at=_hours_ago(100))
        assert priority_score(t_old) > priority_score(t_new)

    def test_age_factor_capped_at_168_hours(self):
        """Score growth plateaus after 1 week."""
        t_1week = _ticket(created_at=_hours_ago(168))
        t_2weeks = _ticket(created_at=_hours_ago(336))
        # Both should have nearly the same age factor (cap at 168h)
        s1 = priority_score(t_1week)
        s2 = priority_score(t_2weeks)
        assert abs(s1 - s2) < 0.01  # capped — virtually identical

    def test_invalid_created_at_falls_back_gracefully(self):
        t = _ticket()
        t.created_at = "not-a-date"
        score = priority_score(t)
        # Should not raise; falls back to age_hours=0.0
        assert score > 0


# ---------------------------------------------------------------------------
# priority_score() — failed attempts penalty
# ---------------------------------------------------------------------------

class TestFailedAttemptsPenalty:
    def test_more_attempts_lower_score(self):
        t_none = _ticket(attempts=[])
        t_one = _ticket(attempts=[{"id": "a1"}])
        t_two = _ticket(attempts=[{"id": "a1"}, {"id": "a2"}])
        assert priority_score(t_none) > priority_score(t_one)
        assert priority_score(t_one) > priority_score(t_two)

    def test_zero_attempts_no_penalty(self):
        t = _ticket(attempts=[])
        score = priority_score(t)
        base = SEVERITY_WEIGHT[TicketSeverity.MEDIUM]
        # fail_factor=1/(1+0*0.4)=1.0 and age_factor≥1.0 so score >= base
        assert score >= base


# ---------------------------------------------------------------------------
# priority_score() — regression boost
# ---------------------------------------------------------------------------

class TestRegressionBoost:
    def test_regression_ticket_scores_higher(self):
        t_normal = _ticket(TicketSeverity.HIGH)
        t_regression = _ticket(TicketSeverity.HIGH, is_regression=True)
        assert priority_score(t_regression) > priority_score(t_normal)

    def test_regression_boost_is_2x(self):
        t_normal = _ticket()
        t_regression = _ticket(is_regression=True)
        # Both at zero age, zero attempts: regression_factor=2.0
        ratio = priority_score(t_regression) / priority_score(t_normal)
        assert abs(ratio - 2.0) < 0.01


# ---------------------------------------------------------------------------
# priority_score() — graph_store integration (mocked)
# ---------------------------------------------------------------------------

class TestGraphStoreFactors:
    def _make_store(
        self,
        similar_count: int = 0,
        cluster: ResolutionCluster | None = None,
        edges: list | None = None,
    ) -> MagicMock:
        store = MagicMock()
        store.count_edges.return_value = similar_count
        store.find_ticket_cluster.return_value = cluster
        store.get_edges.return_value = edges or []
        return store

    def test_similar_edges_boost_score(self):
        t = _ticket()
        store_no = self._make_store(similar_count=0)
        store_yes = self._make_store(similar_count=3)
        assert priority_score(t, store_yes) > priority_score(t, store_no)

    def test_cluster_membership_boosts_score(self):
        t = _ticket()
        cluster = ResolutionCluster(
            cluster_id="c1", ticket_ids=["t1", "t2", "t3", t.ticket_id]
        )
        store_cluster = self._make_store(cluster=cluster)
        store_no = self._make_store(cluster=None)
        assert priority_score(t, store_cluster) > priority_score(t, store_no)

    def test_existing_pr_deprioritises_ticket(self):
        t = _ticket()
        store_pr = self._make_store(edges=[{"edge_type": EdgeType.RESOLVES.value}])
        store_no = self._make_store(edges=[])
        assert priority_score(t, store_pr) < priority_score(t, store_no)

    def test_graph_store_exception_falls_back_gracefully(self):
        t = _ticket()
        store = MagicMock()
        store.count_edges.side_effect = RuntimeError("DB error")
        store.find_ticket_cluster.side_effect = RuntimeError("DB error")
        store.get_edges.side_effect = RuntimeError("DB error")
        # Should not raise; falls back to no graph factors
        score = priority_score(t, store)
        assert score > 0

    def test_no_graph_store_returns_valid_score(self):
        t = _ticket(TicketSeverity.CRITICAL)
        score = priority_score(t, graph_store=None)
        assert score >= SEVERITY_WEIGHT[TicketSeverity.CRITICAL]


# ---------------------------------------------------------------------------
# rank_tickets()
# ---------------------------------------------------------------------------

class TestRankTickets:
    def test_returns_list_of_same_tickets(self):
        tickets = [
            _ticket(TicketSeverity.LOW),
            _ticket(TicketSeverity.HIGH),
            _ticket(TicketSeverity.MEDIUM),
        ]
        ranked = rank_tickets(tickets)
        assert len(ranked) == len(tickets)
        assert set(t.ticket_id for t in ranked) == set(t.ticket_id for t in tickets)

    def test_ranked_order_is_descending_by_severity(self):
        low = _ticket(TicketSeverity.LOW)
        med = _ticket(TicketSeverity.MEDIUM)
        high = _ticket(TicketSeverity.HIGH)
        crit = _ticket(TicketSeverity.CRITICAL)
        # Shuffle order
        ranked = rank_tickets([low, crit, med, high])
        severities = [t.severity for t in ranked]
        assert severities[0] == TicketSeverity.CRITICAL
        assert severities[1] == TicketSeverity.HIGH
        assert severities[2] == TicketSeverity.MEDIUM
        assert severities[3] == TicketSeverity.LOW

    def test_empty_list_returns_empty(self):
        assert rank_tickets([]) == []

    def test_single_ticket_returned_as_list(self):
        t = _ticket(TicketSeverity.CRITICAL)
        ranked = rank_tickets([t])
        assert ranked == [t]

    def test_regression_ticket_outranks_same_severity_normal(self):
        t_normal = _ticket(TicketSeverity.HIGH)
        t_regression = _ticket(TicketSeverity.HIGH, is_regression=True)
        ranked = rank_tickets([t_normal, t_regression])
        assert ranked[0].ticket_id == t_regression.ticket_id

    def test_rank_with_graph_store(self):
        """rank_tickets passes graph_store through to priority_score."""
        t_low = _ticket(TicketSeverity.LOW)
        t_high = _ticket(TicketSeverity.HIGH)
        store = MagicMock()
        store.count_edges.return_value = 0
        store.find_ticket_cluster.return_value = None
        store.get_edges.return_value = []
        ranked = rank_tickets([t_low, t_high], graph_store=store)
        assert ranked[0].severity == TicketSeverity.HIGH

    def test_rank_tickets_handles_scoring_exception(self):
        """If priority_score raises unexpectedly, falls back to severity."""
        t = _ticket(TicketSeverity.CRITICAL)
        t.created_at = None  # could trigger edge case
        # Should not raise
        ranked = rank_tickets([t])
        assert len(ranked) == 1

    def test_multiple_tickets_same_severity_stable(self):
        """All tickets same severity: all are returned, no crash."""
        tickets = [_ticket(TicketSeverity.HIGH) for _ in range(5)]
        ranked = rank_tickets(tickets)
        assert len(ranked) == 5
        for t in ranked:
            assert t.severity == TicketSeverity.HIGH
