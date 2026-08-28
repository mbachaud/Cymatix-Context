"""W2.1: COVER-edge walk — reverse index, walk module, knobs, query wiring.

Spec: docs/research/2026-08-28-wave2-semantic-ranking-graph-research.md
(lane 1) + docs/superpowers/plans/2026-08-28-w2.1-cover-walk.md. The walk
reads the ingest-built ``gene_relations`` COVER graph (relation=5,
entity-overlap kNN, storage/co_activation.py:29-63) — the one populated
gene-gene substrate on bulk-ingested beds — as a query-time ranking signal:
an eps-band-confined rerank class plus an append-below-head rescue lane.
Everything ships default-inert (``cover_walk_enabled = false``).
"""
from __future__ import annotations

import sqlite3

import pytest


def _fresh_conn() -> sqlite3.Connection:
    from cymatix_context.storage.ddl import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


# ═══ Task 1: reverse adjacency index ═══════════════════════════════════


def test_ddl_creates_reverse_index():
    conn = _fresh_conn()
    try:
        names = {r[1] for r in conn.execute("PRAGMA index_list('gene_relations')")}
        assert "idx_gene_relations_rel_b" in names
    finally:
        conn.close()
