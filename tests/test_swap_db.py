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

from helix_context.config import (
    GenomeConfig,
    VaultConfig,
    VaultTracesConfig,
)
from helix_context.knowledge_store import KnowledgeStore
from helix_context.schemas import (
    ChromatinState,
    EpigeneticMarkers,
    Gene,
    PromoterTags,
)
from helix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
    upsert_fingerprint,
)

from tests.conftest import MockCompressorBackend, make_client, make_helix_config


# -- Helpers ---------------------------------------------------------------

# This file's original local mock backend returned pack JSON
# unconditionally for every call (no system-prompt sniffing) — per
# MockCompressorBackend's docstring, that is equivalent to passing this
# payload as ``response=``.
_MOCK_BACKEND_RESPONSE = json.dumps({
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
    config = make_helix_config(
        genome=GenomeConfig(path=genome_path, cold_start_threshold=5),
    )
    client = make_client(
        config=config,
        backend=MockCompressorBackend(response=_MOCK_BACKEND_RESPONSE),
    )
    return client.app, client


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

    def test_swap_db_repoints_registry_genome(self, tmp_path):
        """The session Registry is repointed at the new store after a swap.

        Bug B (2026-05-17): the Registry captures a genome reference at
        app construction and uses ``genome.conn`` directly for every
        read/write — including the background sweep. Before the fix, a
        swap left the Registry holding the OLD store, which the swap then
        closed; the next ``Registry.sweep()`` raised
        ``sqlite3.ProgrammingError: Cannot operate on a closed database``.
        """
        import sqlite3

        db_a = str(tmp_path / "reg_a.db")
        db_b = str(tmp_path / "reg_b.db")
        _make_db(db_a, genes=1).close()
        _make_db(db_b, genes=2).close()

        app, client = _make_app_and_client(db_a)
        registry = app.state.registry

        # Pre-swap: registry points at the boot store.
        assert registry.genome is app.state.helix.genome

        resp = client.post("/admin/swap-db", json={"path": db_b})
        assert resp.status_code == 200

        # Post-swap: registry tracks the new live store, not the closed one.
        assert registry.genome is app.state.helix.genome
        assert registry.genome.path == db_b

        # And the sweep — the background task's payload — runs against the
        # new store without "Cannot operate on a closed database".
        try:
            counts = registry.sweep()
        except sqlite3.ProgrammingError as exc:  # pragma: no cover
            pytest.fail(f"registry.sweep() hit a closed DB after swap: {exc}")
        assert isinstance(counts, dict)

    def test_swap_db_repoints_vault_genome(self, tmp_path):
        """The VaultManager is repointed at the new store after a swap.

        Tier-0 follow-up #4 (2026-05-17): the VaultManager — like the
        session Registry (Bug B, test above) — captures a genome reference
        at app construction (app.py: ``VaultManager(genome=helix.genome)``).
        Its pruner thread calls ``refresh_stale_view(genome=self.genome)``
        on a timer. Before this fix a swap left the VaultManager holding
        the OLD store, which the swap then closed; the next prune cycle
        raised ``sqlite3.ProgrammingError: Cannot operate on a closed
        database``.
        """
        import sqlite3

        db_a = str(tmp_path / "vault_a.db")
        db_b = str(tmp_path / "vault_b.db")
        _make_db(db_a, genes=1).close()
        _make_db(db_b, genes=2).close()

        # Build the app with the vault ENABLED so the pruner payload
        # actually touches the genome (a disabled vault would no-op).
        config = make_helix_config(
            genome=GenomeConfig(path=db_a, cold_start_threshold=5),
        )
        config.vault = VaultConfig(
            enabled=True, path=str(tmp_path / "vault"),
            party_id="", fan_out_threshold=5000,
            redact_body=False, stale_threshold=0.5,
            traces=VaultTracesConfig(
                enabled=True, retention_hours=48,
                max_retention_hours_hard=720, max_count=10000,
                rollup_enabled=True, rollup_shard="hour",
                prune_interval_minutes=60, trigger_only=False,
            ),
        )
        client = make_client(
            config=config,
            backend=MockCompressorBackend(response=_MOCK_BACKEND_RESPONSE),
        )
        app = client.app

        # TestClient is not used as a context manager here, so the app
        # lifespan never runs — start the vault by hand.
        vault = app.state.vault
        vault.start()
        try:
            assert vault._started is True
            # Pre-swap: vault points at the boot store.
            assert vault.genome is app.state.helix.genome

            resp = client.post("/admin/swap-db", json={"path": db_b})
            assert resp.status_code == 200

            # Post-swap: vault tracks the new live store, not the closed one.
            assert vault.genome is app.state.helix.genome
            assert vault.genome.path == db_b

            # The pruner's payload runs against the new store without
            # "Cannot operate on a closed database".
            try:
                results = vault.run_prune_cycle()
            except sqlite3.ProgrammingError as exc:  # pragma: no cover
                pytest.fail(
                    f"vault prune cycle hit a closed DB after swap: {exc}"
                )
            assert isinstance(results, dict)
            assert "stale" in results
        finally:
            vault.stop()


# -- Sharded round-trip via swap-db ----------------------------------------
#
# Issue #98: ``ShardedGenomeAdapter`` was missing several attributes
# (``path``, ``_dense_embedding_enabled``, ``_entity_graph_retrieval_enabled``,
# ``_last_query_scores_lock``, ``query_docs``) that callers read directly off
# ``self.genome``. Most importantly, **every** ``/admin/swap-db`` call
# against a live sharded store hit ``AttributeError: 'ShardedGenomeAdapter'
# object has no attribute 'path'`` at line 858 in routes_admin.py.
#
# These tests cover the swap-A->B->A round trip with ``HELIX_USE_SHARDS=1``
# active so each adapter call surface (path, stats, close) actually fires.


def _build_sharded_layout(root_dir, gene_content: str, domains: list[str],
                         entities: list[str]) -> tuple[str, str]:
    """Create a one-shard sharded layout under ``root_dir``.

    Returns ``(main_path, gene_id)``. ``main_path`` is the
    ``main.genome.db`` that ``open_read_source`` will route through
    ``ShardedGenomeAdapter`` when ``HELIX_USE_SHARDS=1`` is set.
    """
    main_path = str(root_dir / "main.genome.db")
    shard_path = str(root_dir / "shard_a.genome.db")

    # Seed the shard with one doc.
    shard = KnowledgeStore(path=shard_path)
    gene = Gene(
        gene_id="",
        content=gene_content,
        complement=gene_content[:50],
        codons=[],
        promoter=PromoterTags(domains=domains, entities=entities, sequence_index=0),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
        source_id=f"/{domains[0] if domains else 'unknown'}.md",
    )
    gene_id = shard.upsert_doc(gene, apply_gate=False)
    shard.conn.close()
    if getattr(shard, "_reader", None):
        shard._reader.close()

    # Register the shard in main.db.
    main = open_main_db(main_path)
    init_main_db(main)
    register_shard(main, "shard_a", "reference", shard_path, gene_count=1)
    upsert_fingerprint(
        main, gene_id=gene_id, shard_name="shard_a",
        source_id=f"/{domains[0] if domains else 'unknown'}.md",
        domains_json=json.dumps(domains),
        entities_json=json.dumps(entities),
        key_values_json="[]",
    )
    main.close()

    return main_path, gene_id


class TestSwapDbShardedRoundTrip:
    """Round-trip swap-db with HELIX_USE_SHARDS=1 active (issue #98).

    Each test enables sharding via monkeypatch so ``open_read_source``
    returns a ``ShardedGenomeAdapter`` when handed a ``main.genome.db``
    path.
    """

    def test_swap_blob_to_sharded(self, tmp_path, monkeypatch):
        """A -> B where A is blob, B is sharded. After the swap,
        ``helix.genome`` is a ``ShardedGenomeAdapter`` and ``/stats``
        reports the shard's gene count."""
        monkeypatch.setenv("HELIX_USE_SHARDS", "1")

        db_a = str(tmp_path / "blob.db")
        _make_db(db_a, genes=3).close()
        sharded_dir = tmp_path / "sharded"
        sharded_dir.mkdir()
        main_b, _ = _build_sharded_layout(
            sharded_dir, "Helix design doc.", domains=["docs"], entities=["helix"],
        )

        app, client = _make_app_and_client(db_a)

        # Sanity: starts as a blob KnowledgeStore.
        assert isinstance(app.state.helix.genome, KnowledgeStore)
        assert app.state.helix.genome.path == db_a

        resp = client.post("/admin/swap-db", json={"path": main_b})
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["swapped"] is True
        assert data["new_path"] == main_b

        # Now the active store is the sharded adapter.
        from helix_context.sharding import ShardedGenomeAdapter
        assert isinstance(app.state.helix.genome, ShardedGenomeAdapter)
        # And critically: helix.genome.path is reachable (this was the
        # AttributeError that blocked every swap-db once sharded was active).
        assert app.state.helix.genome.path == main_b

    def test_swap_sharded_back_to_blob(self, tmp_path, monkeypatch):
        """The path that originally crashed in #98: an already-sharded
        active store reads ``helix.genome.path`` during swap-db logging.
        With the fix, the swap succeeds and the active store goes back
        to a blob ``KnowledgeStore``."""
        monkeypatch.setenv("HELIX_USE_SHARDS", "1")

        sharded_dir = tmp_path / "sharded"
        sharded_dir.mkdir()
        main_a, _ = _build_sharded_layout(
            sharded_dir, "Sharded source content.",
            domains=["docs"], entities=["helix"],
        )
        db_b = str(tmp_path / "after_swap.db")
        _make_db(db_b, genes=5).close()

        app, client = _make_app_and_client(main_a)

        from helix_context.sharding import ShardedGenomeAdapter
        assert isinstance(app.state.helix.genome, ShardedGenomeAdapter), (
            "App should start with a ShardedGenomeAdapter when "
            "HELIX_USE_SHARDS=1 and genome path ends with main.genome.db"
        )

        # This call previously threw AttributeError: 'ShardedGenomeAdapter'
        # object has no attribute 'path' at routes_admin.py:858 before the
        # fix landed.
        resp = client.post("/admin/swap-db", json={"path": db_b})
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["swapped"] is True
        assert data["old_path"] == main_a
        assert data["new_path"] == db_b
        assert data["genes"] == 5
        # Active store is now a blob KnowledgeStore again.
        assert isinstance(app.state.helix.genome, KnowledgeStore)

    def test_swap_blob_to_sharded_to_blob(self, tmp_path, monkeypatch):
        """Full round-trip: A (blob) -> B (sharded) -> A (blob).

        Exercises every adapter surface that fires during swap:
        ``path``, ``stats``, ``invalidate_sema_cache``,
        ``_build_sema_cache``, ``close``."""
        monkeypatch.setenv("HELIX_USE_SHARDS", "1")

        db_a = str(tmp_path / "blob_a.db")
        _make_db(db_a, genes=2).close()

        sharded_dir = tmp_path / "sharded"
        sharded_dir.mkdir()
        main_b, _ = _build_sharded_layout(
            sharded_dir, "Sharded round-trip content.",
            domains=["auth"], entities=["jwt"],
        )

        app, client = _make_app_and_client(db_a)

        # A -> B
        resp = client.post("/admin/swap-db", json={"path": main_b})
        assert resp.status_code == 200, resp.json()
        from helix_context.sharding import ShardedGenomeAdapter
        assert isinstance(app.state.helix.genome, ShardedGenomeAdapter)

        # B -> A
        resp = client.post("/admin/swap-db", json={"path": db_a})
        assert resp.status_code == 200, resp.json()
        assert resp.json()["genes"] == 2
        assert isinstance(app.state.helix.genome, KnowledgeStore)
        assert app.state.helix.genome.path == db_a
