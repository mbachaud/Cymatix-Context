"""Test that genome bootstrap creates idx_genes_last_seen for incremental export."""
from __future__ import annotations

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
