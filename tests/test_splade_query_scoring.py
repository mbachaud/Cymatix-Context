"""query_splade ranking correctness — bugbash BUG-2.

The candidate pre-filter used to apply SQL LIMIT and min_score against the
UNWEIGHTED per-document term-mass (SUM(weight)) before the query-weighted
dot product was ever computed, so a document with a true weighted score of
20 could be discarded in favor of one scoring 1. These tests pin that both
the LIMIT cutoff and the min_score threshold operate on the query-weighted
dot product.
"""

from __future__ import annotations

import sqlite3

import pytest

from cymatix_context.backends import splade_backend


def _fresh_splade_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    splade_backend.create_splade_table(conn)
    return conn


def _insert_terms(conn, gene_id: str, sparse: dict) -> None:
    conn.executemany(
        "INSERT INTO splade_terms (gene_id, term, weight) VALUES (?, ?, ?)",
        [(gene_id, t, w) for t, w in sparse.items()],
    )
    conn.commit()


def test_query_splade_limit_ranks_by_weighted_dot_product():
    """A doc with high WEIGHTED score must survive the LIMIT cutoff even
    when other docs carry more raw (unweighted) term mass."""
    conn = _fresh_splade_db()
    # gold: raw mass 2.0, weighted score 10.0 * 2.0 = 20.0
    _insert_terms(conn, "gold", {"rare": 2.0})
    # fillers: raw mass 10.0 each, weighted score 0.1 * 10.0 = 1.0
    for i in range(5):
        _insert_terms(conn, f"filler_{i}", {"common": 10.0})

    query_sparse = {"rare": 10.0, "common": 0.1}
    results = splade_backend.query_splade(conn, query_sparse, limit=3)

    assert results, "expected non-empty results"
    top_id, top_score = results[0]
    assert top_id == "gold", f"gold (score 20) outranked by {results}"
    assert top_score == pytest.approx(20.0)
    assert len(results) <= 3


def test_query_splade_min_score_applies_to_weighted_score():
    """A doc whose raw term mass clears min_score but whose weighted dot
    product does not must be excluded."""
    conn = _fresh_splade_db()
    # raw mass 5.0 (> 0.01) but weighted score 0.001 * 5.0 = 0.005 (< 0.01)
    _insert_terms(conn, "weak", {"t": 5.0})

    results = splade_backend.query_splade(conn, {"t": 0.001}, min_score=0.01)
    assert results == []


def test_query_splade_empty_query_returns_empty():
    conn = _fresh_splade_db()
    _insert_terms(conn, "g1", {"a": 1.0})
    assert splade_backend.query_splade(conn, {}) == []


def test_query_splade_scores_sorted_descending():
    conn = _fresh_splade_db()
    _insert_terms(conn, "hi", {"a": 3.0, "b": 1.0})
    _insert_terms(conn, "mid", {"a": 2.0})
    _insert_terms(conn, "lo", {"b": 0.5})

    results = splade_backend.query_splade(conn, {"a": 1.0, "b": 1.0})
    ids = [gid for gid, _ in results]
    scores = [s for _, s in results]
    assert ids == ["hi", "mid", "lo"]
    assert scores == sorted(scores, reverse=True)
