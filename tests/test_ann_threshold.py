"""ANN threshold dynamic gene count tests (Step 4, 2026-05-08)."""
import json
import numpy as np
import pytest
from unittest.mock import patch
from helix_context.schemas import Gene, PromoterTags, EpigeneticMarkers
from helix_context.genome import Genome


def _make(content, domains=None, source=None):
    g = Gene(
        gene_id=Genome.make_gene_id(content),
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=domains or ["test"]),
        epigenetics=EpigeneticMarkers(),
    )
    if source:
        g.source_id = source
    return g


def test_query_genes_ann_returns_list(genome):
    """query_genes_ann must return a list (even with dense_embedding_enabled=False)."""
    g = _make("test ann content", domains=["test"])
    genome.upsert_gene(g)
    result = genome.query_genes_ann("test ann", domains=["test"])
    assert isinstance(result, list)


def test_query_genes_ann_disabled_falls_through(genome):
    """When dense_embedding_enabled=False, result equals query_genes result."""
    g = _make("fallthrough content", domains=["fallthrough"])
    genome.upsert_gene(g)
    genome._dense_embedding_enabled = False
    ann_result = genome.query_genes_ann("fallthrough", domains=["fallthrough"])
    direct_result = genome.query_genes(domains=["fallthrough"], entities=[], max_genes=5)
    assert len(ann_result) == len(direct_result)


def test_query_genes_ann_threshold_respected(genome):
    """Genes with sim < threshold and count >= min_genes must be excluded."""
    genome._dense_embedding_enabled = True
    genome._ann_threshold = 0.9  # very high — almost nothing passes
    genome._ann_min_genes = 1    # but always return at least 1
    for i in range(5):
        g = _make(f"threshold test gene {i}", domains=["threshold_test"])
        genome.upsert_gene(g)

    dim = 4
    query_vec = [1.0, 0.0, 0.0, 0.0]
    # Mock the codec: all genes get sim=0.1, way below threshold=0.9
    mock_codec = type("C", (), {
        "encode": lambda s, text, task="passage": query_vec,
        "similarity": lambda s, a, b: 0.1,
    })()
    genome._dense_codec = mock_codec

    result = genome.query_genes_ann("threshold test", domains=["threshold_test"], min_genes=1)
    # Should return exactly 1 gene (min_genes floor)
    assert len(result) == 1


def test_query_genes_ann_unembedded_genes_honor_min(genome):
    """Un-embedded genes (no embedding_dense) must not cause 0-gene returns."""
    genome._dense_embedding_enabled = True
    genome._ann_threshold = 0.5
    genome._ann_min_genes = 1
    g = _make("unembedded gene content", domains=["unembedded"])
    genome.upsert_gene(g)
    # Don't set embedding_dense — simulate incomplete backfill
    mock_codec = type("C", (), {
        "encode": lambda s, text, task="passage": [1.0, 0.0],
        "similarity": lambda s, a, b: 0.0,
    })()
    genome._dense_codec = mock_codec
    result = genome.query_genes_ann("unembedded", domains=["unembedded"], min_genes=1)
    assert len(result) >= 1
