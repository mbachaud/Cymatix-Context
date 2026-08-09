"""Regression proof: delete_gene sweeps filename_index (2026-08-08 audit).

The audit flagged ``filename_anchor.remove_gene`` as the only cleanup path
for ``filename_index`` and claimed rows leak on document delete because it
has no callers. Refuted: ``KnowledgeStore.delete_gene`` sweeps
``filename_index`` via its ``optional_tables`` loop (knowledge_store.py).
This test pins that sweep so it can't regress silently; the dead
``remove_gene`` was deleted in the same remediation pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cymatix_context.genome import Genome
from tests.conftest import make_gene


def _filename_index_count(g: Genome, gene_id: str) -> int:
    return g.conn.execute(
        "SELECT COUNT(*) FROM filename_index WHERE gene_id = ?", (gene_id,)
    ).fetchone()[0]


def test_delete_gene_sweeps_filename_index(tmp_path):
    db = tmp_path / "genome.db"
    g = Genome(path=str(db), synonym_map={})
    try:
        gene = make_gene(
            "the fusion ranker orders candidates", gene_id="fnidx0000000001"
        )
        gene.source_id = "src/retrieval/fusion_ranker.py"
        g.upsert_gene(gene)
        # Precondition: upsert indexed the filename stem — guards against a
        # vacuous pass if population ever becomes flag-gated.
        assert _filename_index_count(g, "fnidx0000000001") == 1
        assert g.delete_gene("fnidx0000000001")
        assert _filename_index_count(g, "fnidx0000000001") == 0
    finally:
        g.close()
