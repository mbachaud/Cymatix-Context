"""Test that genome bootstrap creates idx_genes_last_seen for incremental export."""
from __future__ import annotations

import time
from pathlib import Path

from helix_context.genome import Genome


def test_idx_genes_last_seen_present(tmp_path: Path):
    g = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
    try:
        rows = g.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_genes_last_seen'"
        ).fetchall()
        assert len(rows) == 1, "idx_genes_last_seen index missing"
    finally:
        g.close()


def test_idx_genes_last_seen_idempotent(tmp_path: Path):
    """Re-opening the genome must not error if index already exists."""
    path = str(tmp_path / "genome.db")
    g1 = Genome(path=path, synonym_map={})
    g1.close()
    g2 = Genome(path=path, synonym_map={})
    try:
        rows = g2.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_genes_last_seen'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        g2.close()


def test_upsert_stamps_last_seen(tmp_path: Path):
    """upsert_gene must populate last_seen with the current Unix epoch."""
    from tests.conftest import make_gene

    g = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
    try:
        before = time.time()
        gid = g.upsert_gene(make_gene("hello"))
        after = time.time()
        row = g.conn.execute(
            "SELECT last_seen FROM genes WHERE gene_id = ?", (gid,)
        ).fetchone()
        assert row[0] is not None, "last_seen must not be NULL after upsert"
        assert before <= row[0] <= after, (
            f"last_seen {row[0]} not in range [{before}, {after}]"
        )
    finally:
        g.close()
