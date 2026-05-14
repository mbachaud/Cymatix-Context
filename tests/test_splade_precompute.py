"""SPLADE precompute plumbing - issue #92, Phase 1.

Verifies that callers can pass a precomputed SPLADE sparse vector to
``sync_splade_index`` and ``upsert_doc`` instead of letting them call
``splade_backend.encode`` inline. Used by the parallel/shard-pool ingest
paths to batch SPLADE encoding outside the per-document upsert.
"""

from __future__ import annotations

import sqlite3

import pytest

from helix_context.backends import splade_backend
from helix_context.storage.indexes import sync_splade_index


def _fresh_splade_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    splade_backend.create_splade_table(conn)
    return conn


def test_sync_splade_index_uses_provided_sparse():
    """When splade_sparse is provided, no inline encode happens."""
    conn = _fresh_splade_db()
    provided = {"alpha": 1.5, "beta": 0.75}

    sync_splade_index(
        conn.cursor(),
        gene_id="g1",
        content="this content should be ignored",
        splade_enabled=True,
        splade_sparse=provided,
    )
    conn.commit()

    rows = conn.execute(
        "SELECT term, weight FROM splade_terms WHERE gene_id = ? ORDER BY term",
        ("g1",),
    ).fetchall()
    assert rows == [("alpha", 1.5), ("beta", 0.75)]


def test_sync_splade_index_disabled_is_noop_even_with_sparse():
    conn = _fresh_splade_db()
    sync_splade_index(
        conn.cursor(),
        gene_id="g1",
        content="x",
        splade_enabled=False,
        splade_sparse={"alpha": 1.0},
    )
    conn.commit()
    rows = conn.execute("SELECT COUNT(*) FROM splade_terms").fetchone()
    assert rows[0] == 0


def test_sync_splade_index_empty_sparse_dict_clears_existing_rows():
    """Pre-existing rows for gene_id get DELETE'd even when sparse is empty."""
    conn = _fresh_splade_db()
    conn.execute(
        "INSERT INTO splade_terms (gene_id, term, weight) VALUES (?, ?, ?)",
        ("g1", "stale", 1.0),
    )
    conn.commit()

    sync_splade_index(
        conn.cursor(),
        gene_id="g1",
        content="x",
        splade_enabled=True,
        splade_sparse={},
    )
    conn.commit()

    rows = conn.execute(
        "SELECT COUNT(*) FROM splade_terms WHERE gene_id = ?", ("g1",)
    ).fetchone()
    assert rows[0] == 0
