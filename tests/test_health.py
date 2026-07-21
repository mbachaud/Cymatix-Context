"""
Tests for the delta-epsilon context health monitor and HGT.
"""

import json
import pytest
import tempfile
import os

from cymatix_context.context_manager import HelixContextManager
from cymatix_context.genome import Genome
from cymatix_context.hgt import export_genome, import_genome, genome_diff
from cymatix_context.schemas import ContextHealth

from tests.conftest import make_gene, make_helix_config, MockCompressorBackend


@pytest.fixture
def health_helix():
    config = make_helix_config(synonym_map={"auth": ["jwt", "login", "security"]})
    mgr = HelixContextManager(config)
    # MockCompressorBackend's splice branch returns codon indices for
    # "Gene <id>" prompts, so these tests exercise codons-kept assembly
    # (the old local mock returned {} -> complement fallback; that path
    # keeps dedicated coverage in test_ribosome.py). Assertions here are
    # threshold-based and hold under either assembly mode.
    mgr.ribosome.backend = MockCompressorBackend()
    yield mgr
    mgr.close()


@pytest.fixture
def seeded_health_helix(health_helix):
    genes = [
        make_gene("JWT authentication middleware",
                  domains=["auth", "security"], entities=["jwt"],
                  gene_id="auth_gene_001"),
        make_gene("Database connection pooling",
                  domains=["database", "performance"], entities=["postgres"],
                  gene_id="db_gene_0001"),
        make_gene("React component state management",
                  domains=["frontend", "react"], entities=["useState"],
                  gene_id="react_gene_01"),
    ]
    for g in genes:
        health_helix.genome.upsert_gene(g)
    return health_helix


# ═══════════════════════════════════════════════════════════════════
# Delta-Epsilon Health Monitor
# ═══════════════════════════════════════════════════════════════════


class TestContextHealth:
    def test_empty_genome_is_sparse(self, health_helix):
        window = health_helix.build_context("anything")
        assert window.context_health.status in ("denatured", "sparse")
        assert window.context_health.genes_expressed == 0
        assert window.context_health.ellipticity == 0.0

    def test_matching_query_has_coverage(self, seeded_health_helix):
        window = seeded_health_helix.build_context("How does JWT auth work?")
        health = window.context_health
        assert health.genes_expressed >= 1
        assert health.coverage > 0
        assert health.freshness > 0
        assert health.ellipticity > 0
        # With mock backend, density is very low (tiny spliced output vs 6k budget)
        # so ellipticity may be low. Just verify the signals are populated.
        assert health.genes_available == 3

    def test_no_match_shows_denatured(self, seeded_health_helix):
        """Query lexically disjoint from the genome → denatured health.

        Tier-0 PR-3 (2026-05-16) decoupled BGE-M3 dense recall from
        ``fusion_mode``, so ``query_docs`` now runs dense recall in the
        default additive mode. ``query_docs_dense_recall`` has no minimum
        cosine cutoff — it returns the top-k by cosine — so a content-full
        genome surfaces a few weakly-similar genes for *any* query. The
        pre-PR-3 ``genes_expressed == 0`` assertion encoded the dense-dark
        world where the additive path never touched dense vectors; that is
        no longer reachable for a non-empty genome (true zero-retrieval is
        covered by ``test_empty_genome_is_sparse``).

        The substantive invariant the health monitor must still uphold:
        a lexically-disjoint query yields near-zero ``ellipticity`` and a
        ``denatured`` status — the dense neighbours are weak enough that
        retrieval quality is correctly flagged as bad.
        """
        window = seeded_health_helix.build_context("quantum entanglement physics")
        health = window.context_health
        assert health.genes_available == 3
        assert health.status == "denatured"
        # Dense recall may surface a few weak semantic neighbours; the
        # health signal must still classify this retrieval as denatured,
        # which requires ellipticity below the 0.3 'sparse' threshold.
        assert health.ellipticity < 0.3, (
            f"lexically-disjoint query must yield denatured-grade "
            f"ellipticity; got {health.ellipticity}"
        )

    def test_health_in_metadata(self, seeded_health_helix):
        window = seeded_health_helix.build_context("auth security jwt")
        assert hasattr(window, "context_health")
        health = window.context_health
        assert isinstance(health, ContextHealth)
        assert 0 <= health.ellipticity <= 1
        assert 0 <= health.coverage <= 1
        assert 0 <= health.density <= 1
        assert 0 <= health.freshness <= 1

    def test_freshness_reflects_decay(self, seeded_health_helix):
        """Genes with low decay scores should reduce freshness."""
        # Manually decay a gene
        gene = seeded_health_helix.genome.get_gene("auth_gene_001")
        gene.epigenetics.decay_score = 0.2
        seeded_health_helix.genome.upsert_gene(gene)

        window = seeded_health_helix.build_context("auth security")
        health = window.context_health
        # Freshness should be lower since one gene is stale
        assert health.freshness < 1.0

    def test_health_genes_available_count(self, seeded_health_helix):
        window = seeded_health_helix.build_context("auth")
        assert window.context_health.genes_available == 3


# ═══════════════════════════════════════════════════════════════════
# Horizontal Gene Transfer (HGT)
# ═══════════════════════════════════════════════════════════════════


class TestHGTExport:
    def test_export_creates_file(self):
        genome = Genome(":memory:")
        genome.upsert_gene(make_gene("test content", domains=["test"], gene_id="gene_001"))

        with tempfile.NamedTemporaryFile(suffix=".helix", delete=False) as f:
            path = f.name

        try:
            result = export_genome(genome, path, description="Test export")
            assert result["genes"] == 1
            assert result["file_size"] > 0
            assert os.path.exists(path)

            data = json.loads(open(path, encoding="utf-8").read())
            assert data["helix_format_version"] == 1
            assert data["header"]["gene_count"] == 1
            assert data["header"]["description"] == "Test export"
            assert len(data["genes"]) == 1
        finally:
            os.unlink(path)
            genome.close()

    def test_export_excludes_heterochromatin_by_default(self):
        genome = Genome(":memory:")
        from cymatix_context.schemas import ChromatinState
        genome.upsert_gene(make_gene("active", domains=["test"], gene_id="active_1"))
        genome.upsert_gene(make_gene("stale", domains=["test"], gene_id="stale_1",
                                     chromatin=ChromatinState.HETEROCHROMATIN))

        with tempfile.NamedTemporaryFile(suffix=".helix", delete=False) as f:
            path = f.name

        try:
            result = export_genome(genome, path)
            assert result["genes"] == 1  # Only active gene

            result_all = export_genome(genome, path, include_heterochromatin=True)
            assert result_all["genes"] == 2  # Both genes
        finally:
            os.unlink(path)
            genome.close()


class TestHGTImport:
    def test_import_into_empty_genome(self):
        # Export from source. Use content-addressed gene_ids so import_genome's
        # tamper check passes.
        source = Genome(":memory:")
        content_a = "shared knowledge"
        content_b = "more knowledge"
        gid_a = Genome.make_gene_id(content_a)
        gid_b = Genome.make_gene_id(content_b)
        source.upsert_gene(make_gene(content_a, domains=["test"]))
        source.upsert_gene(make_gene(content_b, domains=["test"]))

        with tempfile.NamedTemporaryFile(suffix=".helix", delete=False) as f:
            path = f.name

        try:
            export_genome(source, path)
            source.close()

            # Import into target
            target = Genome(":memory:")
            result = import_genome(target, path)
            assert result["imported"] == 2
            assert result["skipped"] == 0

            assert target.get_gene(gid_a) is not None
            assert target.get_gene(gid_b) is not None
            target.close()
        finally:
            os.unlink(path)


class TestHGTDiff:
    def test_diff_shows_differences(self):
        source = Genome(":memory:")
        source.upsert_gene(make_gene("shared", domains=["test"], gene_id="shared_1"))
        source.upsert_gene(make_gene("only in file", domains=["test"], gene_id="file_only"))

        with tempfile.NamedTemporaryFile(suffix=".helix", delete=False) as f:
            path = f.name

        try:
            export_genome(source, path)
            source.close()

            target = Genome(":memory:")
            target.upsert_gene(make_gene("shared", domains=["test"], gene_id="shared_1"))
            target.upsert_gene(make_gene("only in genome", domains=["test"], gene_id="genome_only"))

            result = genome_diff(target, path)
            assert result["shared"] == 1
            assert result["only_in_file"] == 1
            assert result["only_in_genome"] == 1
            target.close()
        finally:
            os.unlink(path)
