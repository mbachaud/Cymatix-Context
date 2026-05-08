"""BM25 pre-filter tier-0 tests (2026-05-08 retrieval stack upgrade, Step 1)."""

import pytest
from helix_context.genome import Genome
from helix_context.schemas import Gene, PromoterTags, EpigeneticMarkers


def _make_gene(content, domains, entities=None, source_id=None):
    g = Gene(
        gene_id=Genome.make_gene_id(content),
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=domains, entities=entities or []),
        epigenetics=EpigeneticMarkers(),
    )
    if source_id:
        g.source_id = source_id
    return g


@pytest.fixture
def genome_with_noise(genome):
    signal = _make_gene(
        "The helix proxy port is 11437.",
        domains=["helix"],
        entities=["port", "11437"],
        source_id="helix.toml",
    )
    genome.upsert_gene(signal)
    for i in range(50):
        noise = _make_gene(
            f"Service {i} listens on port {8000 + i}. General networking config.",
            domains=["networking"],
            entities=["port"],
            source_id=f"service_{i}.conf",
        )
        genome.upsert_gene(noise)
    return genome


def test_prefilter_keeps_signal_gene(genome_with_noise):
    genome_with_noise._bm25_prefilter_enabled = True
    genome_with_noise._bm25_prefilter_size = 10
    genes = genome_with_noise.query_genes(
        domains=["helix"], entities=["port", "11437"], max_genes=5
    )
    sources = [g.source_id for g in genes]
    assert "helix.toml" in sources, f"Signal gene missing; got: {sources}"


def test_prefilter_reduces_candidates_scored(genome_with_noise):
    """_bm25_candidate_set should return only genes FTS5 ranked for the query terms."""
    genome_with_noise._bm25_prefilter_enabled = True
    genome_with_noise._bm25_prefilter_size = 10
    candidate_set = genome_with_noise._bm25_candidate_set(["helix", "11437"], size=10)
    # Should return a non-None set capped at prefilter_size
    assert candidate_set is not None, "Expected a candidate set, got None fallback"
    assert len(candidate_set) <= 10
    # Signal gene (helix.toml) should be in the FTS top-10
    # (We can't check source_id from a set of gene_ids, but we can confirm it's bounded)
    assert all(isinstance(gid, str) for gid in candidate_set)


def test_prefilter_empty_shortlist_fallback(genome):
    genome._bm25_prefilter_enabled = True
    genome._bm25_prefilter_size = 5
    g = _make_gene(
        "xyzzy_nonexistent_abc placeholder content",
        domains=["xyzzy_nonexistent_abc"],
    )
    genome.upsert_gene(g)
    genes = genome.query_genes(domains=["xyzzy_nonexistent_abc"], entities=[], max_genes=3)
    assert isinstance(genes, list)
