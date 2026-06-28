"""WS3: symbol-graph personalized PageRank (candidate-local, pure CPU)."""
from helix_context.scoring import symbol_pagerank as spr


def test_widely_referenced_definition_is_central():
    # A, B, D all reference C's definition -> C is the central node.
    edges = [("A", "C"), ("B", "C"), ("D", "C"), ("A", "B")]
    nodes, adj = spr.build_adjacency(edges)
    r = spr.personalized_pagerank(nodes, adj)
    assert r["C"] == max(r.values())
    assert abs(sum(r.values()) - 1.0) < 1e-6


def test_personalization_raises_target():
    edges = [("A", "C"), ("B", "C")]
    nodes, adj = spr.build_adjacency(edges)
    base = spr.personalized_pagerank(nodes, adj)
    boosted = spr.personalized_pagerank(nodes, adj, {"A": 100.0})
    assert boosted["A"] > base["A"]


def test_dangling_node_conserves_mass():
    # C is dangling (no out-edges); mass must not leak.
    edges = [("A", "C"), ("B", "C")]
    nodes, adj = spr.build_adjacency(edges)
    r = spr.personalized_pagerank(nodes, adj)
    assert abs(sum(r.values()) - 1.0) < 1e-6


def test_empty_and_single_node():
    assert spr.personalized_pagerank([], {}) == {}
    assert spr.personalized_pagerank(["X"], {"X": []}) == {"X": 1.0}


def test_symbol_centrality_ranks_referenced_candidate_higher():
    # C is referenced by A, B, D; A is peripheral. Among candidates {A, C}, C wins.
    edges = [("A", "C"), ("B", "C"), ("D", "C")]
    cent = spr.symbol_centrality(["A", "C"], edges)
    assert cent["C"] > cent["A"]


def test_symbol_centrality_restricts_to_candidates():
    edges = [("A", "C"), ("B", "C")]
    cent = spr.symbol_centrality(["A", "C"], edges)
    assert set(cent) == {"A", "C"}  # neighbour-only nodes not returned


def test_symbol_centrality_empty():
    assert spr.symbol_centrality([], []) == {}


def test_session_weight_dominates_query_weight():
    nodes = ["s", "q", "n"]
    w = spr.build_personalization(nodes, query_symbol_nodes={"q"}, session_nodes={"s"})
    assert w["s"] > w["q"] > w["n"]
