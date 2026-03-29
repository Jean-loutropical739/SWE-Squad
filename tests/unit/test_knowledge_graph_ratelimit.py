"""Tests for src/unknown/knowledge_graph.py — cosine mismatch + max_pairs throttling.

Covers:
- _cosine_similarity returning None on dimension mismatch (not raising)
- auto_link_by_similarity respecting the max_pairs cap
- Skipped duplicate pairs not counting against max_pairs
"""

from __future__ import annotations

import unittest

from src.unknown.knowledge_graph import (
    EdgeType,
    KnowledgeGraph,
    KnowledgeNode,
    NodeType,
    _cosine_similarity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_node(ext_id: str, emb: list) -> KnowledgeNode:
    return KnowledgeNode(NodeType.TICKET, ext_id, f"ticket-{ext_id}", embedding=emb)


# ---------------------------------------------------------------------------
# _cosine_similarity: dimension mismatch
# ---------------------------------------------------------------------------

class TestAutoLinkMismatchedEmbeddings(unittest.TestCase):
    """_cosine_similarity returns None on mismatched dimensions; auto_link skips silently."""

    def test_cosine_similarity_raises_on_mismatch(self):
        """Old behaviour raised ValueError; new behaviour returns None instead."""
        # Should NOT raise — must return None
        result = _cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])
        self.assertIsNone(result)

    def test_cosine_similarity_returns_none_on_mismatch(self):
        result = _cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0])
        self.assertIsNone(result)

    def test_mismatched_embeddings_skipped(self):
        """Nodes with different embedding dimensions are silently skipped."""
        g = KnowledgeGraph(similarity_threshold=0.0)
        g.add_node(_make_node("a", [1.0, 0.0]))
        g.add_node(_make_node("b", [1.0, 0.0, 0.0]))
        edges = g.auto_link_by_similarity()
        self.assertEqual(len(edges), 0)

    def test_mixed_valid_and_mismatched(self):
        """Valid pairs are linked even when some pairs have mismatched dimensions."""
        g = KnowledgeGraph(similarity_threshold=0.0)
        # a and b have dim=2; c has dim=3 — a-b pair should link, a-c and b-c skip
        g.add_node(_make_node("a", [1.0, 0.0]))
        g.add_node(_make_node("b", [1.0, 0.0]))
        g.add_node(_make_node("c", [1.0, 0.0, 0.0]))
        edges = g.auto_link_by_similarity()
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].edge_type, EdgeType.SIMILAR)


# ---------------------------------------------------------------------------
# auto_link_by_similarity: max_pairs cap
# ---------------------------------------------------------------------------

class TestAutoLinkMaxPairs(unittest.TestCase):
    """auto_link_by_similarity respects the max_pairs cap."""

    def test_max_pairs_limits_evaluations(self):
        """max_pairs=2 caps evaluation at 2 pairs out of 6 possible."""
        g = KnowledgeGraph(similarity_threshold=0.0)
        # 4 nodes -> 6 pairs; all embeddings are identical so all would link
        for i in range(4):
            g.add_node(_make_node(str(i), [1.0, 0.0, 0.0]))
        edges = g.auto_link_by_similarity(max_pairs=2)
        self.assertLessEqual(len(edges), 2)

    def test_max_pairs_larger_than_total_pairs(self):
        """max_pairs larger than the number of pairs behaves like unlimited."""
        g = KnowledgeGraph(similarity_threshold=0.0)
        emb = [1.0, 0.0]
        for i in range(3):
            g.add_node(_make_node(str(i), emb))
        # 3 nodes -> 3 pairs; max_pairs=100 should link all
        edges = g.auto_link_by_similarity(max_pairs=100)
        self.assertEqual(len(edges), 3)

    def test_max_pairs_zero_means_unlimited(self):
        """max_pairs=0 means no cap (unlimited)."""
        g = KnowledgeGraph(similarity_threshold=0.0)
        emb = [1.0, 0.0]
        for i in range(4):
            g.add_node(_make_node(str(i), emb))
        edges = g.auto_link_by_similarity(max_pairs=0)
        # 4 nodes -> 6 pairs; 0 = unlimited -> all 6 linked
        self.assertEqual(len(edges), 6)

    def test_default_max_pairs_is_unlimited(self):
        """When max_pairs is not supplied, all pairs are evaluated."""
        g = KnowledgeGraph(similarity_threshold=0.0)
        emb = [1.0, 0.0]
        for i in range(4):
            g.add_node(_make_node(str(i), emb))
        edges = g.auto_link_by_similarity()
        self.assertEqual(len(edges), 6)

    def test_max_pairs_one(self):
        """max_pairs=1 produces at most 1 edge."""
        g = KnowledgeGraph(similarity_threshold=0.0)
        emb = [1.0, 0.0]
        for i in range(4):
            g.add_node(_make_node(str(i), emb))
        edges = g.auto_link_by_similarity(max_pairs=1)
        self.assertLessEqual(len(edges), 1)


# ---------------------------------------------------------------------------
# max_pairs interaction with already-existing (skipped) pairs
# ---------------------------------------------------------------------------

class TestAutoLinkMaxPairsWithSkips(unittest.TestCase):
    """Skipped duplicate pairs do not count against max_pairs."""

    def test_skipped_duplicates_not_counted(self):
        """Pairs skipped because SIMILAR edge already exists don't consume the cap."""
        g = KnowledgeGraph(similarity_threshold=0.0)
        emb = [1.0, 0.0]
        nodes = []
        for i in range(3):
            n = _make_node(str(i), emb)
            g.add_node(n)
            nodes.append(n)

        # Pre-link node 0 -> node 1
        g.add_edge(nodes[0].node_id, nodes[1].node_id, EdgeType.SIMILAR, weight=1.0)

        # With max_pairs=1, the pre-existing pair is skipped (not counted).
        # The first real pair evaluated should be 0->2 or 1->2, yielding 1 new edge.
        edges = g.auto_link_by_similarity(max_pairs=1)
        self.assertEqual(len(edges), 1)


if __name__ == "__main__":
    unittest.main()
