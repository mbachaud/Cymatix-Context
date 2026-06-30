"""
Symbol-graph personalized PageRank (WS3).

Ranks code chunks by *structural centrality* over the WS2 symbol graph, then
the assembler trims the candidate set to budget by this centrality. This is
Aider's repo-map recipe pointed at Helix's graph: a definition that many
retrieved chunks reference is structurally central and should be kept.

Edge direction: a ``SYMBOL_REF`` edge runs referencing-chunk -> defining-chunk,
so PageRank mass flows *to* definitions — a widely-referenced definition
accumulates rank from its referencers and rises. "Personalized" = bias the
random-walk restart toward chunks that define/reference identifiers in the
user's query (Aider's 10x) and chunks already delivered this session (Aider's
50x), so centrality is query-relative, not global.

Pure CPU, no model, no neural inference — graph arithmetic over a
**candidate-local** subgraph (the retrieved set + its 1-hop neighbourhood), so
it stays cheap and respects Helix's no-query-time-inference posture. Pure-Python
(no numpy dependency), following the cymatics.py / tcm.py pattern.

This is an additive fusion tier *under* the lexical tiers, never a gate — an
exact lexical hit is never displaced by centrality (lexical-first, PRD s4).
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("helix.symbol_pagerank")

# Aider-style restart weights for the personalization vector.
QUERY_SYMBOL_WEIGHT = 10.0    # chunk defines/references an identifier in the query
SESSION_WEIGHT = 50.0         # chunk already delivered this session (in-context)
BASE_WEIGHT = 1.0             # every candidate gets a small uniform restart mass


def build_adjacency(
    edges: Iterable[Tuple[str, str]],
    nodes: Optional[Iterable[str]] = None,
) -> Tuple[List[str], Dict[str, List[str]]]:
    """From directed (referencer, definition) edges -> (node_list, out_adjacency).

    Edges referencing a node not in ``nodes`` (when given) are dropped so the
    graph stays candidate-local. With ``nodes=None`` the node set is inferred
    from the edges.
    """
    edges = list(edges)  # materialize once — safe for generator arguments
    node_set = set(nodes) if nodes is not None else set()
    if nodes is None:
        for a, b in edges:
            node_set.add(a)
            node_set.add(b)
    out: Dict[str, List[str]] = {n: [] for n in node_set}
    for a, b in edges:
        if a in node_set and b in node_set and a != b:
            out[a].append(b)
    return sorted(node_set), out


def personalized_pagerank(
    nodes: Sequence[str],
    out_adjacency: Dict[str, List[str]],
    personalization: Optional[Dict[str, float]] = None,
    damping: float = 0.85,
    max_iter: int = 50,
    tol: float = 1.0e-6,
) -> Dict[str, float]:
    """Personalized PageRank by power iteration. Returns {node: score}, sum 1.

    ``personalization`` is the restart distribution (un-normalized weights are
    fine; normalized internally). Missing/empty -> uniform. Dangling nodes
    (no out-edges) redistribute their mass via the restart vector, so no rank
    is lost.
    """
    n = len(nodes)
    if n == 0:
        return {}
    if n == 1:
        return {nodes[0]: 1.0}

    # restart distribution p
    if personalization:
        total = sum(max(0.0, personalization.get(node, 0.0)) for node in nodes)
        if total <= 0.0:
            p = {node: 1.0 / n for node in nodes}
        else:
            p = {node: max(0.0, personalization.get(node, 0.0)) / total for node in nodes}
    else:
        p = {node: 1.0 / n for node in nodes}

    rank = {node: 1.0 / n for node in nodes}
    out_deg = {node: len(out_adjacency.get(node, ())) for node in nodes}

    for _ in range(max_iter):
        # mass stuck on dangling nodes -> redistributed via restart vector
        dangling = sum(rank[node] for node in nodes if out_deg[node] == 0)
        new = {node: (1.0 - damping) * p[node] + damping * dangling * p[node]
               for node in nodes}
        for node in nodes:
            deg = out_deg[node]
            if deg == 0:
                continue
            share = damping * rank[node] / deg
            for nb in out_adjacency[node]:
                new[nb] += share
        delta = sum(abs(new[node] - rank[node]) for node in nodes)
        rank = new
        if delta < tol:
            break

    # numerical renormalization
    s = sum(rank.values()) or 1.0
    return {node: rank[node] / s for node in nodes}


def build_personalization(
    nodes: Sequence[str],
    query_symbol_nodes: Optional[Iterable[str]] = None,
    session_nodes: Optional[Iterable[str]] = None,
) -> Dict[str, float]:
    """Aider-style restart weights: base for all, +query-symbol, +session."""
    qset = set(query_symbol_nodes or ())
    sset = set(session_nodes or ())
    weights: Dict[str, float] = {}
    for node in nodes:
        w = BASE_WEIGHT
        if node in qset:
            w += QUERY_SYMBOL_WEIGHT
        if node in sset:
            w += SESSION_WEIGHT
        weights[node] = w
    return weights


def symbol_centrality(
    candidate_ids: Sequence[str],
    edges: Iterable[Tuple[str, str]],
    query_symbol_nodes: Optional[Iterable[str]] = None,
    session_nodes: Optional[Iterable[str]] = None,
    damping: float = 0.85,
) -> Dict[str, float]:
    """Convenience: centrality over the candidate-local symbol graph.

    ``edges`` are (referencer_gene_id, definition_gene_id) SYMBOL_REF edges among
    the candidates + their 1-hop neighbourhood (the caller fetches them). Returns
    a {gene_id: centrality} map restricted to ``candidate_ids`` (the scores the
    fusion tier consumes); neighbour-only nodes participate in the walk but are
    not returned for scoring.
    """
    cand = list(dict.fromkeys(candidate_ids))  # de-dup, keep order
    if not cand:
        return {}
    nodes, out_adj = build_adjacency(edges, nodes=None)
    # ensure every candidate is a node even if it has no edges
    node_set = set(nodes) | set(cand)
    for c in cand:
        out_adj.setdefault(c, [])
    nodes = sorted(node_set)
    if len(nodes) <= 1:
        return {c: 0.0 for c in cand}
    pers = build_personalization(nodes, query_symbol_nodes, session_nodes)
    ranks = personalized_pagerank(nodes, out_adj, pers, damping=damping)
    return {c: ranks.get(c, 0.0) for c in cand}
