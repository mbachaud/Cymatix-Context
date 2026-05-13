"""Tests for the hot-swap .db feature.

Covers:
  - read_only flag on KnowledgeStore (upsert_doc, link_coactivated no-ops)
  - POST /admin/swap-db endpoint (swap, reject missing, read_only flag)
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from fastapi.testclient import TestClient

from helix_context.config import (
    BudgetConfig,
    GenomeConfig,
    HelixConfig,
    RibosomeConfig,
    ServerConfig,
)
from helix_context.knowledge_store import KnowledgeStore
from helix_context.schemas import Gene
from helix_context.server import create_app


# -- Helpers ---------------------------------------------------------------


class _MockBackend:
    """Minimal ribosome backend — returns valid JSON for all calls."""

    def complete(self, prompt: str, system: str = "", temperature: float = 0.0) -> str:
        return json.dumps({
            "codons": [{"meaning": "test", "weight": 1.0, "is_exon": True}],
            "complement": "compressed",
            "promoter": {
                "domains": ["test"],
                "entities": [],
                "intent": "test",
                "summary": "test",
            },
        })


def _make_gene(content: str = "hello world") -> Gene:
    gene_id = KnowledgeStore.make_gene_id(content)
    return Gene(
        gene_id=gene_id,
        content=content,
        complement="compressed",
        codons=["test"],
    )


def _make_db(path: str, genes: int = 0) -> KnowledgeStore:
    """Create a KnowledgeStore at *path* and optionally seed *genes* docs."""
    ks = KnowledgeStore(path=path)
    for i in range(genes):
        g = _make_gene(f"document-{i}-{path}")
        ks.upsert_doc(g, apply_gate=False)
    return ks


# -- read_only unit tests --------------------------------------------------


class TestReadOnlyFlag:
    def test_read_only_upsert_noop(self, tmp_path):
        """upsert_doc returns a gene_id but does not write to the db."""
        db_path = str(tmp_path / "ro.db")
        ks = KnowledgeStore(path=db_path, read_only=True)

        gene = _make_gene("should not be persisted")
        gene_id = ks.upsert_doc(gene, apply_gate=False)

        # gene_id must still be returned
        assert gene_id
        assert len(gene_id) == 16

        # But nothing was written
        total = ks.stats()["total_genes"]
        assert total == 0
        ks.close()

    def test_read_only_link_coactivated_noop(self, tmp_path):
        """link_coactivated does not write harmonic links."""
        db_path = str(tmp_path / "ro_link.db")
        ks = KnowledgeStore(path=db_path, read_only=True)

        # Should not raise and should not write anything
        ks.link_coactivated(["aaa", "bbb", "ccc"])

        # Verify no harmonic_links rows
        row = ks.conn.execute(
            "SELECT COUNT(*) FROM harmonic_links"
        ).fetchone()
        assert row[0] == 0
        ks.close()

    def test_read_only_touch_genes_noop(self, tmp_path):
        """touch_genes returns immediately without updating epigenetics."""
        db_path = str(tmp_path / "ro_touch.db")
        ks = KnowledgeStore(path=db_path)

        # Seed a doc first (writable), then switch to read_only
        gene = _make_gene("touchable doc")
        gene_id = ks.upsert_doc(gene, apply_gate=False)

        ks.read_only = True
        ks.touch_genes([gene_id])

        # Verify epigenetics unchanged — access_count should still be 0
        row = ks.conn.execute(
            "SELECT epigenetics FROM genes WHERE gene_id = ?", (gene_id,)
        ).fetchone()
        if row and row[0]:
            import json as _json
            epi = _json.loads(row[0])
            assert epi.get("access_count", 0) == 0
        ks.close()

    def test_read_only_store_harmonic_weights_noop(self, tmp_path):
        """store_harmonic_weights is a no-op in read_only mode."""
        db_path = str(tmp_path / "ro_harm.db")
        ks = KnowledgeStore(path=db_path, read_only=True)

        ks.store_harmonic_weights([("a", "b", 0.5)])

        row = ks.conn.execute(
            "SELECT COUNT(*) FROM harmonic_links"
        ).fetchone()
        assert row[0] == 0
        ks.close()

    def test_read_only_log_health_noop(self, tmp_path):
        """log_health is a no-op in read_only mode."""
        db_path = str(tmp_path / "ro_health.db")
        ks = KnowledgeStore(path=db_path, read_only=True)

        ks.log_health(
            query="test", ellipticity=0.5, coverage=0.5,
            density=0.5, freshness=0.5, genes_expressed=1,
            genes_available=10, status="ok",
        )

        row = ks.conn.execute(
            "SELECT COUNT(*) FROM health_log"
        ).fetchone()
        assert row[0] == 0
        ks.close()


# -- /admin/swap-db endpoint tests -----------------------------------------


def _make_app_and_client(genome_path: str):
    """Build a FastAPI app + TestClient with a real KnowledgeStore at *genome_path*."""
    config = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=4),
        genome=GenomeConfig(path=genome_path, cold_start_threshold=5),
        server=ServerConfig(upstream="http://localhost:11434"),
    )
    app = create_app(config)
    app.state.helix.ribosome.backend = _MockBackend()
    return app, TestClient(app)


class TestSwapDbEndpoint:
    def test_swap_db_endpoint_swaps_genome(self, tmp_path):
        """Swap between two dbs and verify gene count changes."""
        db_a = str(tmp_path / "a.db")
        db_b = str(tmp_path / "b.db")

        # Seed db_a with 3 genes, db_b with 7 genes
        ks_a = _make_db(db_a, genes=3)
        ks_a.close()
        ks_b = _make_db(db_b, genes=7)
        ks_b.close()

        app, client = _make_app_and_client(db_a)

        # Verify initial state
        resp = client.get("/stats")
        assert resp.json()["total_genes"] == 3

        # Swap to db_b
        resp = client.post("/admin/swap-db", json={"path": db_b})
        assert resp.status_code == 200
        data = resp.json()
        assert data["swapped"] is True
        assert data["genes"] == 7
        assert data["old_path"] == db_a
        assert data["new_path"] == db_b
        assert "elapsed_ms" in data

        # Verify /stats now reflects the new db
        resp = client.get("/stats")
        assert resp.json()["total_genes"] == 7

    def test_swap_db_endpoint_rejects_missing_path(self, tmp_path):
        """Return 400 when the target path does not exist."""
        db_a = str(tmp_path / "exists.db")
        _make_db(db_a, genes=1).close()

        _, client = _make_app_and_client(db_a)

        resp = client.post(
            "/admin/swap-db",
            json={"path": str(tmp_path / "nonexistent.db")},
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["error"]

    def test_swap_db_endpoint_rejects_empty_path(self, tmp_path):
        """Return 400 when path is empty."""
        db_a = str(tmp_path / "base.db")
        _make_db(db_a, genes=1).close()

        _, client = _make_app_and_client(db_a)

        resp = client.post("/admin/swap-db", json={"path": ""})
        assert resp.status_code == 400

    def test_swap_db_endpoint_read_only_flag(self, tmp_path):
        """Swapped genome has read_only=True when requested."""
        db_a = str(tmp_path / "rw.db")
        db_b = str(tmp_path / "ro.db")
        _make_db(db_a, genes=1).close()
        _make_db(db_b, genes=2).close()

        app, client = _make_app_and_client(db_a)

        resp = client.post(
            "/admin/swap-db",
            json={"path": db_b, "read_only": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["swapped"] is True
        assert data["read_only"] is True

        # Verify the actual genome object has read_only set
        assert app.state.helix.genome.read_only is True

    def test_swap_db_endpoint_read_only_default_false(self, tmp_path):
        """read_only defaults to False when omitted."""
        db_a = str(tmp_path / "default_a.db")
        db_b = str(tmp_path / "default_b.db")
        _make_db(db_a, genes=1).close()
        _make_db(db_b, genes=1).close()

        app, client = _make_app_and_client(db_a)

        resp = client.post("/admin/swap-db", json={"path": db_b})
        assert resp.status_code == 200
        assert resp.json()["read_only"] is False
        assert app.state.helix.genome.read_only is False
