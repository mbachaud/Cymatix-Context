"""WS2 review FIX-2: symbol rows must not outlive their genes.

Two lifecycle paths:
  1. Explicit deletion — ``delete_gene()`` (the API promised by the
     compress_to_heterochromatin docstring) must remove the document's
     ``symbol_defs`` rows and its ``gene_relations`` edges in BOTH
     directions along with the genes row and per-gene index rows.
  2. Orphan sweep — genes removed out-of-band (external scripts, raw SQL)
     leave orphaned symbol rows behind; the compaction passes
     (``compact()`` / ``compact_genome()``, the /admin/compact path) must
     sweep ``symbol_defs`` / SYMBOL_REF rows whose gene_id no longer
     exists in ``genes``.
"""
from __future__ import annotations

import pytest

from cymatix_context.encoding import tree_chunker as tc
from cymatix_context.genome import Genome
from cymatix_context.schemas import StructuralRelation
from tests.conftest import make_gene

SYMBOL_REF = int(StructuralRelation.SYMBOL_REF)


def _symbol_rows(conn, gene_id: str):
    defs = conn.execute(
        "SELECT COUNT(*) FROM symbol_defs WHERE gene_id = ?", (gene_id,)
    ).fetchone()[0]
    edges = conn.execute(
        "SELECT COUNT(*) FROM gene_relations "
        "WHERE gene_id_a = ? OR gene_id_b = ?",
        (gene_id, gene_id),
    ).fetchone()[0]
    return defs, edges


class TestDeleteGeneCleansSymbolRows:
    def test_delete_gene_removes_defs_and_edges_both_directions(self):
        g = Genome(path=":memory:", synonym_map={})
        try:
            a = make_gene("def alpha_target(): pass", domains=["code"])
            b = make_gene(
                "def bravo_caller():\n    return alpha_target()", domains=["code"]
            )
            g.upsert_gene(a)
            g.upsert_gene(b)
            g.store_symbol_defs(
                [("alpha_target", a.gene_id, "def"), ("bravo_caller", b.gene_id, "def")]
            )
            # One edge per direction so BOTH columns are exercised.
            g.store_relations_batch(
                [
                    (b.gene_id, a.gene_id, SYMBOL_REF, 1.0),
                    (a.gene_id, b.gene_id, SYMBOL_REF, 1.0),
                ]
            )

            assert g.delete_gene(a.gene_id) is True

            defs_a, edges_a = _symbol_rows(g.conn, a.gene_id)
            assert defs_a == 0, "symbol_defs rows survived gene deletion"
            assert edges_a == 0, (
                "SYMBOL_REF edges survived gene deletion (must be cleaned "
                "in both directions)"
            )
            # The surviving gene keeps its own rows.
            defs_b, _ = _symbol_rows(g.conn, b.gene_id)
            assert defs_b == 1
            assert g.conn.execute(
                "SELECT 1 FROM genes WHERE gene_id = ?", (a.gene_id,)
            ).fetchone() is None
        finally:
            g.close()

    def test_delete_gene_missing_id_returns_false(self):
        g = Genome(path=":memory:", synonym_map={})
        try:
            assert g.delete_gene("nosuchgene000001") is False
        finally:
            g.close()


class TestCompactionOrphanSweep:
    def _seed_orphans(self, g: Genome):
        """A live gene with symbol rows + orphaned rows for a gene that was
        removed out-of-band (raw SQL, mirroring external cleanup scripts)."""
        live = make_gene("def live_fn(): pass", domains=["code"])
        dead = make_gene("def dead_fn(): pass", domains=["code"])
        g.upsert_gene(live)
        g.upsert_gene(dead)
        g.store_symbol_defs(
            [("live_fn", live.gene_id, "def"), ("dead_fn", dead.gene_id, "def")]
        )
        g.store_relations_batch(
            [
                (live.gene_id, dead.gene_id, SYMBOL_REF, 1.0),
                (dead.gene_id, live.gene_id, SYMBOL_REF, 1.0),
            ]
        )
        # Out-of-band removal: bypasses delete_gene on purpose.
        g.conn.execute("DELETE FROM genes WHERE gene_id = ?", (dead.gene_id,))
        g.conn.commit()
        return live, dead

    def test_compact_sweeps_orphaned_symbol_rows(self):
        g = Genome(path=":memory:", synonym_map={})
        try:
            live, dead = self._seed_orphans(g)
            g.compact()
            defs_dead, edges_dead = _symbol_rows(g.conn, dead.gene_id)
            assert defs_dead == 0, "compact() left orphaned symbol_defs rows"
            assert edges_dead == 0, "compact() left orphaned SYMBOL_REF edges"
            defs_live, _ = _symbol_rows(g.conn, live.gene_id)
            assert defs_live == 1, "compact() swept a LIVE gene's symbol rows"
        finally:
            g.close()

    def test_compact_genome_sweeps_orphaned_symbol_rows(self):
        g = Genome(path=":memory:", synonym_map={})
        try:
            live, dead = self._seed_orphans(g)
            g.compact_genome(dry_run=False)
            defs_dead, edges_dead = _symbol_rows(g.conn, dead.gene_id)
            assert defs_dead == 0, "compact_genome() left orphaned symbol_defs rows"
            assert edges_dead == 0, "compact_genome() left orphaned SYMBOL_REF edges"
            defs_live, _ = _symbol_rows(g.conn, live.gene_id)
            assert defs_live == 1
        finally:
            g.close()

    def test_compact_genome_dry_run_does_not_sweep(self):
        g = Genome(path=":memory:", synonym_map={})
        try:
            _live, dead = self._seed_orphans(g)
            g.compact_genome(dry_run=True)
            defs_dead, edges_dead = _symbol_rows(g.conn, dead.gene_id)
            assert defs_dead == 1, "dry_run must not modify the store"
            assert edges_dead == 2, "dry_run must not modify the store"
        finally:
            g.close()


# ---------------------------------------------------------------------------
# End-to-end: real ingest through the symbol-aware chunker, then delete.
# ---------------------------------------------------------------------------

_BIG = "\n".join(f"    step_{i} = {i} * 2" for i in range(400))
_CODE = (
    "def compute_tax(amount):\n" + _BIG + "\n    return amount * 0.2\n\n\n"
    "def invoice_total(items):\n    sub = sum(items)\n    return sub + compute_tax(sub)\n"
)


@pytest.mark.skipif(
    not tc.is_available(), reason="tree-sitter (+ tree-sitter-python) not installed"
)
def test_ingest_then_delete_leaves_zero_symbol_rows(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIX_USE_SHARDS", raising=False)
    monkeypatch.setenv("HELIX_GENOME_PATH", str(tmp_path / "genome.db"))
    from cymatix_context.config import load_config
    from cymatix_context.context_manager import HelixContextManager

    cfg = load_config()
    cfg.ingestion.symbol_graph = True
    cfg.ingestion.sema_embed_on_ingest = False
    mgr = HelixContextManager(cfg)
    mgr.ingest(_CODE, content_type="code",
               metadata={"path": "billing.py", "source_id": "billing.py"})

    conn = mgr.genome.conn
    sym_gids = [r[0] for r in conn.execute(
        "SELECT DISTINCT gene_id FROM symbol_defs"
    ).fetchall()]
    assert sym_gids, "ingest produced no symbol_defs rows (fixture broken)"
    n_edges = conn.execute(
        "SELECT COUNT(*) FROM gene_relations WHERE relation = ?", (SYMBOL_REF,)
    ).fetchone()[0]
    assert n_edges > 0, "ingest produced no SYMBOL_REF edges (fixture broken)"

    for gid in sym_gids:
        mgr.genome.delete_gene(gid)

    assert conn.execute("SELECT COUNT(*) FROM symbol_defs").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM gene_relations WHERE relation = ?", (SYMBOL_REF,)
    ).fetchone()[0] == 0
