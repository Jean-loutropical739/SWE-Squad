"""Unit tests for src/swe_team/batch_resolution.py.

Covers:
- _cosine_similarity helper
- on_ticket_resolved() — happy path, cap enforcement, skip already-resolved siblings,
  no cluster, no embedding, exception tolerance
- discover_cluster() — already clustered, joins existing, creates new, too few matches
- populate_related_tickets() — happy path, both edge directions, dedup, empty edges

Note: batch_resolution.py imports embed_ticket locally inside on_ticket_resolved()
via `from src.swe_team.embeddings import embed_ticket`, so we patch at the source
module: src.swe_team.embeddings.embed_ticket.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import (
    EdgeType,
    KnowledgeEdge,
    ResolutionCluster,
    SWETicket,
    TicketSeverity,
    TicketStatus,
)
from src.swe_team.batch_resolution import (
    AUTO_RESOLVE_THRESHOLD,
    CLUSTER_THRESHOLD,
    MAX_AUTO_RESOLVE,
    _cosine_similarity,
    discover_cluster,
    on_ticket_resolved,
    populate_related_tickets,
)

# Patch target: embed_ticket lives in the embeddings module and is imported at
# call time inside batch_resolution functions.
_EMBED_PATCH = "src.swe_team.embeddings.embed_ticket"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ticket(ticket_id: str = "t-main", status: TicketStatus = TicketStatus.OPEN, **kwargs) -> SWETicket:
    defaults = dict(
        ticket_id=ticket_id,
        title="Test bug",
        description="Something broke",
        severity=TicketSeverity.MEDIUM,
        status=status,
        investigation_report="Root cause: xyz. " * 20,
        metadata={"resolution_note": "fix_succeeded"},
    )
    defaults.update(kwargs)
    return SWETicket(**defaults)


def _resolved_ticket(ticket_id: str = "t-main") -> SWETicket:
    return _ticket(ticket_id=ticket_id, status=TicketStatus.RESOLVED)


def _mock_store(tickets: dict | None = None) -> MagicMock:
    store = MagicMock()
    tickets = tickets or {}

    def _get(tid):
        return tickets.get(tid)

    store.get.side_effect = _get
    store.find_similar.return_value = []
    store.add = MagicMock()
    return store


def _mock_graph(cluster: ResolutionCluster | None = None) -> MagicMock:
    graph = MagicMock()
    graph.find_ticket_cluster.return_value = cluster
    graph.create_cluster = MagicMock()
    graph.add_ticket_to_cluster = MagicMock()
    graph.get_edges.return_value = []
    return graph


# ---------------------------------------------------------------------------
# _cosine_similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self):
        assert _cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_empty_vector_returns_zero(self):
        assert _cosine_similarity([], []) == 0.0

    def test_mismatched_length_returns_zero(self):
        assert _cosine_similarity([1.0], [1.0, 2.0]) == 0.0

    def test_high_similarity(self):
        a = [0.9, 0.1, 0.0]
        b = [0.85, 0.15, 0.01]
        result = _cosine_similarity(a, b)
        assert result > 0.99


# ---------------------------------------------------------------------------
# on_ticket_resolved
# ---------------------------------------------------------------------------

class TestOnTicketResolved:
    def test_no_cluster_returns_empty(self):
        ticket = _resolved_ticket()
        store = _mock_store()
        graph = _mock_graph(cluster=None)

        result = on_ticket_resolved(ticket, store, graph)

        assert result == []
        store.get.assert_not_called()

    def test_single_ticket_cluster_returns_empty(self):
        cluster = ResolutionCluster(
            cluster_id="c1",
            ticket_ids=["t-main"],
        )
        ticket = _resolved_ticket()
        store = _mock_store()
        graph = _mock_graph(cluster=cluster)

        with patch(_EMBED_PATCH, return_value=[1.0, 0.0]):
            result = on_ticket_resolved(ticket, store, graph)

        assert result == []

    def test_no_embedding_returns_empty(self):
        cluster = ResolutionCluster(cluster_id="c1", ticket_ids=["t-main", "t-sib"])
        ticket = _resolved_ticket()
        store = _mock_store()
        graph = _mock_graph(cluster=cluster)

        with patch(_EMBED_PATCH, return_value=None):
            result = on_ticket_resolved(ticket, store, graph)

        assert result == []

    def test_sibling_auto_resolved_via_direct_similarity(self):
        """Sibling not found in find_similar results → falls back to direct embed compare."""
        cluster = ResolutionCluster(cluster_id="c1", ticket_ids=["t-main", "t-sib"])
        ticket = _resolved_ticket()
        sibling = _ticket(
            ticket_id="t-sib",
            status=TicketStatus.OPEN,
            metadata={"resolution_note": "fix_succeeded"},
        )
        store = _mock_store(tickets={"t-sib": sibling})
        store.find_similar.return_value = []
        graph = _mock_graph(cluster=cluster)

        shared_embedding = [1.0, 0.0, 0.0]

        with patch(_EMBED_PATCH, return_value=shared_embedding):
            result = on_ticket_resolved(ticket, store, graph)

        assert "t-sib" in result
        assert sibling.status == TicketStatus.RESOLVED
        assert sibling.metadata.get("auto_resolved_by") == "t-main"

    def test_sibling_auto_resolved_via_find_similar(self):
        """Sibling found in find_similar results (high-similarity match)."""
        cluster = ResolutionCluster(cluster_id="c1", ticket_ids=["t-main", "t-sib"])
        ticket = _resolved_ticket()
        sibling = _ticket(
            ticket_id="t-sib",
            status=TicketStatus.OPEN,
            metadata={"resolution_note": "fix_succeeded"},
        )
        store = _mock_store(tickets={"t-sib": sibling})
        store.find_similar.return_value = [
            {"ticket_id": "t-sib", "raw_similarity": 0.95}
        ]
        graph = _mock_graph(cluster=cluster)

        with patch(_EMBED_PATCH, return_value=[1.0, 0.0]):
            result = on_ticket_resolved(ticket, store, graph)

        assert "t-sib" in result

    def test_already_resolved_sibling_is_skipped(self):
        cluster = ResolutionCluster(cluster_id="c1", ticket_ids=["t-main", "t-sib"])
        ticket = _resolved_ticket()
        sibling = _ticket(
            ticket_id="t-sib",
            status=TicketStatus.RESOLVED,
            metadata={"resolution_note": "fix_succeeded"},
        )
        store = _mock_store(tickets={"t-sib": sibling})
        graph = _mock_graph(cluster=cluster)

        with patch(_EMBED_PATCH, return_value=[1.0, 0.0]):
            result = on_ticket_resolved(ticket, store, graph)

        assert result == []

    def test_acknowledged_sibling_is_skipped(self):
        cluster = ResolutionCluster(cluster_id="c1", ticket_ids=["t-main", "t-sib"])
        ticket = _resolved_ticket()
        sibling = _ticket(ticket_id="t-sib", status=TicketStatus.ACKNOWLEDGED, metadata={})
        store = _mock_store(tickets={"t-sib": sibling})
        graph = _mock_graph(cluster=cluster)

        with patch(_EMBED_PATCH, return_value=[1.0, 0.0]):
            result = on_ticket_resolved(ticket, store, graph)

        assert result == []

    def test_max_auto_resolve_cap_enforced(self):
        """With cap=2 and 5 siblings, only 2 get auto-resolved."""
        sibling_ids = [f"t-s{i}" for i in range(5)]
        cluster = ResolutionCluster(cluster_id="c1", ticket_ids=["t-main"] + sibling_ids)
        ticket = _resolved_ticket()

        siblings = {
            sid: _ticket(
                ticket_id=sid,
                status=TicketStatus.OPEN,
                metadata={"resolution_note": "fix_succeeded"},
            )
            for sid in sibling_ids
        }
        store = _mock_store(tickets=siblings)
        store.find_similar.return_value = []
        graph = _mock_graph(cluster=cluster)

        with patch(_EMBED_PATCH, return_value=[1.0, 0.0]):
            result = on_ticket_resolved(ticket, store, graph, max_auto_resolve=2)

        assert len(result) == 2

    def test_low_similarity_sibling_not_resolved(self):
        """Sibling with low cosine similarity should NOT be auto-resolved."""
        cluster = ResolutionCluster(cluster_id="c1", ticket_ids=["t-main", "t-sib"])
        ticket = _resolved_ticket()
        sibling = _ticket(
            ticket_id="t-sib",
            status=TicketStatus.OPEN,
            metadata={"resolution_note": "fix_succeeded"},
        )
        store = _mock_store(tickets={"t-sib": sibling})
        store.find_similar.return_value = []
        graph = _mock_graph(cluster=cluster)

        resolved_emb = [1.0, 0.0, 0.0]
        sibling_emb = [0.0, 1.0, 0.0]  # orthogonal → similarity = 0

        def _fake_embed(t):
            if t.ticket_id == "t-main":
                return resolved_emb
            return sibling_emb

        with patch(_EMBED_PATCH, side_effect=_fake_embed):
            result = on_ticket_resolved(ticket, store, graph)

        assert result == []

    def test_exception_in_store_does_not_crash(self):
        cluster = ResolutionCluster(cluster_id="c1", ticket_ids=["t-main", "t-sib"])
        ticket = _resolved_ticket()
        store = _mock_store()
        store.get.side_effect = RuntimeError("DB error")
        graph = _mock_graph(cluster=cluster)

        with patch(_EMBED_PATCH, return_value=[1.0, 0.0]):
            result = on_ticket_resolved(ticket, store, graph)

        assert result == []

    def test_cluster_status_updated_to_resolved_when_all_done(self):
        """When all siblings get auto-resolved, cluster.status becomes 'resolved'."""
        cluster = ResolutionCluster(cluster_id="c1", ticket_ids=["t-main", "t-sib"])
        ticket = _resolved_ticket()
        sibling = _ticket(
            ticket_id="t-sib",
            status=TicketStatus.OPEN,
            metadata={"resolution_note": "fix_succeeded"},
        )
        store = _mock_store(tickets={"t-sib": sibling})
        store.find_similar.return_value = []
        graph = _mock_graph(cluster=cluster)

        with patch(_EMBED_PATCH, return_value=[1.0, 0.0]):
            on_ticket_resolved(ticket, store, graph)

        assert cluster.status == "resolved"
        graph.create_cluster.assert_called_once_with(cluster)


# ---------------------------------------------------------------------------
# discover_cluster
# ---------------------------------------------------------------------------

class TestDiscoverCluster:
    def test_already_in_cluster_returns_existing_id(self):
        existing = ResolutionCluster(cluster_id="c-exist", ticket_ids=["t-main"])
        ticket = _ticket()
        store = _mock_store()
        graph = _mock_graph(cluster=existing)

        result = discover_cluster(ticket, [1.0, 0.0], store, graph)

        assert result == "c-exist"
        store.find_similar.assert_not_called()

    def test_no_similar_tickets_returns_none(self):
        ticket = _ticket()
        store = _mock_store()
        store.find_similar.return_value = []
        graph = _mock_graph(cluster=None)

        result = discover_cluster(ticket, [1.0, 0.0], store, graph)

        assert result is None

    def test_similar_ticket_already_in_cluster_joins_it(self):
        ticket = _ticket(ticket_id="t-new")
        similar_id = "t-old"
        existing_cluster = ResolutionCluster(cluster_id="c-old", ticket_ids=[similar_id])

        store = _mock_store()
        store.find_similar.return_value = [
            {"ticket_id": similar_id, "raw_similarity": 0.92}
        ]

        def _find_cluster(tid):
            if tid == similar_id:
                return existing_cluster
            return None

        graph = MagicMock()
        graph.find_ticket_cluster.side_effect = _find_cluster
        graph.add_ticket_to_cluster = MagicMock()

        result = discover_cluster(ticket, [1.0, 0.0], store, graph)

        assert result == "c-old"
        graph.add_ticket_to_cluster.assert_called_once_with("c-old", "t-new")

    def test_creates_new_cluster_when_no_existing(self):
        ticket = _ticket(ticket_id="t-new", source_module="auth.py")
        similar_id = "t-similar"

        store = _mock_store()
        store.find_similar.return_value = [
            {"ticket_id": similar_id, "raw_similarity": 0.92}
        ]

        graph = MagicMock()
        graph.find_ticket_cluster.return_value = None
        graph.create_cluster = MagicMock()

        result = discover_cluster(ticket, [1.0, 0.0], store, graph)

        assert result is not None
        assert len(result) == 12  # uuid4().hex[:12]
        graph.create_cluster.assert_called_once()
        cluster_arg = graph.create_cluster.call_args[0][0]
        assert "t-new" in cluster_arg.ticket_ids
        assert similar_id in cluster_arg.ticket_ids
        assert cluster_arg.primary_module == "auth.py"

    def test_self_is_filtered_from_similar(self):
        """The ticket itself must not be included in similar_ids."""
        ticket = _ticket(ticket_id="t-self")

        store = _mock_store()
        store.find_similar.return_value = [
            {"ticket_id": "t-self", "raw_similarity": 0.99}
        ]

        graph = MagicMock()
        graph.find_ticket_cluster.return_value = None
        graph.create_cluster = MagicMock()

        result = discover_cluster(ticket, [1.0, 0.0], store, graph)

        assert result is None
        graph.create_cluster.assert_not_called()

    def test_exception_returns_none(self):
        ticket = _ticket()
        store = _mock_store()
        store.find_similar.side_effect = RuntimeError("network error")
        graph = _mock_graph(cluster=None)

        result = discover_cluster(ticket, [1.0, 0.0], store, graph)

        assert result is None

    def test_below_threshold_similar_filtered_out(self):
        """Matches below cluster_threshold must not form a cluster."""
        ticket = _ticket(ticket_id="t-new")

        store = _mock_store()
        store.find_similar.return_value = [
            {"ticket_id": "t-low", "raw_similarity": 0.50}  # below 0.85
        ]

        graph = MagicMock()
        graph.find_ticket_cluster.return_value = None
        graph.create_cluster = MagicMock()

        result = discover_cluster(ticket, [1.0, 0.0], store, graph,
                                  cluster_threshold=0.85)

        assert result is None
        graph.create_cluster.assert_not_called()


# ---------------------------------------------------------------------------
# populate_related_tickets
# ---------------------------------------------------------------------------

class TestPopulateRelatedTickets:
    def test_empty_edges_returns_empty(self):
        ticket = _ticket(ticket_id="t-main")
        graph = _mock_graph()
        graph.get_edges.return_value = []

        result = populate_related_tickets(ticket, graph)

        assert result == []
        assert ticket.related_tickets == []

    def test_source_edges_populates_targets(self):
        ticket = _ticket(ticket_id="t-main")
        edges = [
            KnowledgeEdge(source_id="t-main", target_id="t-rel1", edge_type=EdgeType.SIMILAR),
            KnowledgeEdge(source_id="t-main", target_id="t-rel2", edge_type=EdgeType.SIMILAR),
        ]
        graph = _mock_graph()
        graph.get_edges.return_value = edges

        result = populate_related_tickets(ticket, graph)

        assert "t-rel1" in result
        assert "t-rel2" in result
        assert len(result) == 2

    def test_target_edges_populates_sources(self):
        """When the ticket appears as the target, the source is added."""
        ticket = _ticket(ticket_id="t-main")
        edges = [
            KnowledgeEdge(source_id="t-other", target_id="t-main", edge_type=EdgeType.SIMILAR),
        ]
        graph = _mock_graph()
        graph.get_edges.return_value = edges

        result = populate_related_tickets(ticket, graph)

        assert "t-other" in result

    def test_self_loops_not_included(self):
        ticket = _ticket(ticket_id="t-main")
        edges = [
            KnowledgeEdge(source_id="t-main", target_id="t-main", edge_type=EdgeType.SIMILAR),
        ]
        graph = _mock_graph()
        graph.get_edges.return_value = edges

        result = populate_related_tickets(ticket, graph)

        assert result == []

    def test_duplicates_are_deduplicated(self):
        ticket = _ticket(ticket_id="t-main")
        edges = [
            KnowledgeEdge(source_id="t-main", target_id="t-rel", edge_type=EdgeType.SIMILAR),
            KnowledgeEdge(source_id="t-rel", target_id="t-main", edge_type=EdgeType.SIMILAR),
        ]
        graph = _mock_graph()
        graph.get_edges.return_value = edges

        result = populate_related_tickets(ticket, graph)

        assert result.count("t-rel") == 1

    def test_cap_at_20(self):
        ticket = _ticket(ticket_id="t-main")
        edges = [
            KnowledgeEdge(source_id="t-main", target_id=f"t-{i}", edge_type=EdgeType.SIMILAR)
            for i in range(25)
        ]
        graph = _mock_graph()
        graph.get_edges.return_value = edges

        result = populate_related_tickets(ticket, graph)

        assert len(result) <= 20
        assert ticket.related_tickets == result

    def test_exception_returns_empty(self):
        ticket = _ticket(ticket_id="t-main")
        graph = MagicMock()
        graph.get_edges.side_effect = RuntimeError("graph down")

        result = populate_related_tickets(ticket, graph)

        assert result == []

    def test_ticket_field_updated_in_place(self):
        ticket = _ticket(ticket_id="t-main")
        edges = [
            KnowledgeEdge(source_id="t-main", target_id="t-rel", edge_type=EdgeType.SIMILAR),
        ]
        graph = _mock_graph()
        graph.get_edges.return_value = edges

        populate_related_tickets(ticket, graph)

        assert ticket.related_tickets == ["t-rel"]
