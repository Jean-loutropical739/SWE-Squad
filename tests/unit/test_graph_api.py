"""
Tests for the ticket similarity graph API endpoint (issue #133).

Covers:
  - /api/graph returns valid structure with nodes/edges/heatmap
  - Empty ticket store returns empty graph (no 500)
  - Heatmap cell values are non-negative integers
  - Node severity values are valid enum strings
  - Edge similarity scores are within expected range
  - Node count capped at 50
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── Project bootstrap ────────────────────────────────────────────────────────
logging.logAsyncioTasks = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.ticket_store import TicketStore


def _import_build_graph_data():
    """Import _build_graph_data from dashboard_server."""
    from scripts.ops.dashboard_server import _build_graph_data
    return _build_graph_data


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

VALID_SEVERITIES = {"critical", "high", "medium", "low"}


@pytest.fixture
def empty_store(tmp_path):
    """A ticket store with no tickets."""
    return TicketStore(path=str(tmp_path / "empty_tickets.json"))


@pytest.fixture
def populated_store(tmp_path):
    """A ticket store with a mix of tickets across severities and modules."""
    store = TicketStore(path=str(tmp_path / "tickets.json"))
    now = datetime.now(timezone.utc)
    tickets = [
        SWETicket(
            ticket_id="T-001",
            title="xyzzy frobnicator wibble wobble quux corge",
            description="Timeout during login",
            severity=TicketSeverity.CRITICAL,
            status=TicketStatus.OPEN,
            source_module="browser",
            created_at=(now - timedelta(days=1)).isoformat(),
            metadata={"affected_modules": ["browser", "auth"]},
        ),
        SWETicket(
            ticket_id="T-002",
            title="xyzzy frobnicator wibble wobble quux corge",
            description="Timeout during login crash",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.OPEN,
            source_module="browser",
            created_at=(now - timedelta(days=2)).isoformat(),
            metadata={"affected_modules": ["browser"]},
        ),
        SWETicket(
            ticket_id="T-003",
            title="Auth token refresh failure",
            description="Token expired and not refreshed",
            severity=TicketSeverity.MEDIUM,
            status=TicketStatus.RESOLVED,
            source_module="auth",
            created_at=(now - timedelta(days=5)).isoformat(),
            metadata={"affected_modules": ["auth", "api"]},
        ),
        SWETicket(
            ticket_id="T-004",
            title="API rate limit exceeded",
            description="Hit rate limit on external API",
            severity=TicketSeverity.LOW,
            status=TicketStatus.OPEN,
            source_module="api",
            created_at=(now - timedelta(days=10)).isoformat(),
        ),
        SWETicket(
            ticket_id="T-005",
            title="Browser page rendering failure",
            description="Page did not render",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.OPEN,
            source_module="browser",
            created_at=now.isoformat(),
        ),
    ]
    for t in tickets:
        store.add(t)
    return store


# ══════════════════════════════════════════════════════════════════════════════
# Tests — Structure
# ══════════════════════════════════════════════════════════════════════════════

class TestGraphAPIStructure:
    """Validate the top-level structure of /api/graph responses."""

    def test_returns_valid_structure(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        assert "nodes" in data
        assert "edges" in data
        assert "heatmap" in data
        assert isinstance(data["nodes"], list)
        assert isinstance(data["edges"], list)
        assert isinstance(data["heatmap"], dict)

    def test_heatmap_has_modules_and_cells(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        hm = data["heatmap"]
        assert "modules" in hm
        assert "cells" in hm
        assert isinstance(hm["modules"], list)
        assert isinstance(hm["cells"], list)
        # cells should be a square matrix matching module count
        assert len(hm["cells"]) == len(hm["modules"])
        for row in hm["cells"]:
            assert len(row) == len(hm["modules"])

    def test_node_fields(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        for node in data["nodes"]:
            assert "id" in node
            assert "title" in node
            assert "severity" in node
            assert "module" in node
            assert "created_days_ago" in node

    def test_edge_fields(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        for edge in data["edges"]:
            assert "source" in edge
            assert "target" in edge
            assert "similarity" in edge


# ══════════════════════════════════════════════════════════════════════════════
# Tests — Empty store
# ══════════════════════════════════════════════════════════════════════════════

class TestGraphAPIEmpty:
    """Ensure empty ticket store does not cause errors."""

    def test_empty_store_returns_empty_graph(self, empty_store):
        build = _import_build_graph_data()
        data = build(empty_store)
        assert data["nodes"] == []
        assert data["edges"] == []
        assert data["heatmap"]["modules"] == []
        assert data["heatmap"]["cells"] == []

    def test_empty_store_no_exception(self, empty_store):
        build = _import_build_graph_data()
        # Should not raise
        data = build(empty_store)
        assert isinstance(data, dict)


# ══════════════════════════════════════════════════════════════════════════════
# Tests — Heatmap values
# ══════════════════════════════════════════════════════════════════════════════

class TestGraphAPIHeatmap:
    """Validate heatmap cell values."""

    def test_heatmap_cells_non_negative_integers(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        for row in data["heatmap"]["cells"]:
            for val in row:
                assert isinstance(val, int)
                assert val >= 0

    def test_heatmap_diagonal_counts_self_module(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        hm = data["heatmap"]
        # The browser module has 3 tickets; diagonal entry should be > 0
        if "browser" in hm["modules"]:
            idx = hm["modules"].index("browser")
            assert hm["cells"][idx][idx] > 0


# ══════════════════════════════════════════════════════════════════════════════
# Tests — Node severity
# ══════════════════════════════════════════════════════════════════════════════

class TestGraphAPINodeSeverity:
    """Validate node severity values."""

    def test_node_severities_valid(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        for node in data["nodes"]:
            assert node["severity"] in VALID_SEVERITIES, (
                f"Invalid severity: {node['severity']}"
            )

    def test_node_count_matches_store(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        assert len(data["nodes"]) == 5


# ══════════════════════════════════════════════════════════════════════════════
# Tests — Edges
# ══════════════════════════════════════════════════════════════════════════════

class TestGraphAPIEdges:
    """Validate edge similarity values."""

    def test_edge_similarity_range(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        for edge in data["edges"]:
            assert 0.75 < edge["similarity"] <= 1.0, (
                f"Edge similarity out of range: {edge['similarity']}"
            )

    def test_similar_titles_produce_edges(self, populated_store):
        """T-001 and T-002 have very similar titles — should be linked."""
        build = _import_build_graph_data()
        data = build(populated_store)
        edge_pairs = {(e["source"], e["target"]) for e in data["edges"]}
        edge_pairs |= {(e["target"], e["source"]) for e in data["edges"]}
        assert ("T-001", "T-002") in edge_pairs or ("T-002", "T-001") in edge_pairs

    def test_edge_sources_are_valid_node_ids(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        node_ids = {n["id"] for n in data["nodes"]}
        for edge in data["edges"]:
            assert edge["source"] in node_ids
            assert edge["target"] in node_ids


# ══════════════════════════════════════════════════════════════════════════════
# Tests — Node cap
# ══════════════════════════════════════════════════════════════════════════════

class TestGraphAPINodeCap:
    """Ensure node count is capped at 50."""

    def test_max_50_nodes(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "many_tickets.json"))
        now = datetime.now(timezone.utc)
        for i in range(60):
            t = SWETicket(
                ticket_id=f"T-{i:03d}",
                title=f"Issue number {i}",
                description=f"Description for issue {i}",
                severity=TicketSeverity.MEDIUM,
                source_module="mod",
                created_at=(now - timedelta(days=i)).isoformat(),
            )
            store.add(t)
        build = _import_build_graph_data()
        data = build(store)
        assert len(data["nodes"]) <= 50


# ══════════════════════════════════════════════════════════════════════════════
# Tests — JSON serializable
# ══════════════════════════════════════════════════════════════════════════════

class TestGraphAPISerialization:
    """Ensure the response is JSON-serializable."""

    def test_json_serializable(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        # Should not raise
        serialized = json.dumps(data, default=str)
        parsed = json.loads(serialized)
        assert parsed["nodes"] == data["nodes"]

    def test_created_days_ago_is_integer(self, populated_store):
        build = _import_build_graph_data()
        data = build(populated_store)
        for node in data["nodes"]:
            assert isinstance(node["created_days_ago"], int)
