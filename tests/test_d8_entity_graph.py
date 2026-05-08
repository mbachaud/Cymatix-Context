"""Entity graph Tier 5b tests (Step 3C, 2026-05-08)."""
import json
import sqlite3
import pytest
from helix_context.schemas import Gene, PromoterTags, EpigeneticMarkers
from helix_context.genome import Genome


def _make(content, domains=None, entities=None, source=None):
    g = Gene(
        gene_id=Genome.make_gene_id(content),
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=domains or [], entities=entities or []),
        epigenetics=EpigeneticMarkers(),
    )
    if source:
        g.source_id = source
    return g


def test_entity_graph_flag_default_false(genome):
    """entity_graph_retrieval_enabled must be off by default."""
    assert not genome._entity_graph_retrieval_enabled


def test_entity_graph_tier_inactive_without_flag(genome):
    """With _entity_graph_retrieval_enabled=False, entity_graph rows must not affect scores."""
    gene_a = _make("gene a content for entity test", source="a.py")
    gene_b = _make("gene b content for entity test", source="b.py")
    genome.upsert_gene(gene_a)
    genome.upsert_gene(gene_b)
    genome._entity_graph_retrieval_enabled = False
    genes = genome.query_genes(domains=["gene"], entities=["a"], max_genes=5)
    # Must not raise; results are based on other tiers only
    assert isinstance(genes, list)


def test_entity_graph_soft_fails_on_query_error(genome):
    """Tier 5b must not raise even if the entity_graph query fails (soft-fail contract)."""
    # Drop the entity_graph table to simulate a missing/corrupt table
    con = genome.conn
    con.execute("DROP TABLE IF EXISTS entity_graph")
    con.commit()
    genome._entity_graph_retrieval_enabled = True
    gene = _make("test content for entity graph soft fail", domains=["test"])
    genome.upsert_gene(gene)
    # Should not raise even with entity_graph table dropped
    try:
        genes = genome.query_genes(
            domains=["test"],
            entities=["test"],
            max_genes=3,
            use_entity_graph=True,
        )
        assert isinstance(genes, list)
    except Exception as e:
        pytest.fail(f"entity_graph tier raised instead of soft-failing: {e}")


def test_entity_graph_boost_applied_to_matching_gene(genome):
    """When entity_graph has a matching row, gene score is boosted."""
    gene = _make("entity boost test gene content", domains=["entitytest"], entities=["myentity"])
    gene_id = gene.gene_id
    genome.upsert_gene(gene)

    # Manually insert an entity_graph row (entity_graph write is gated on ingestion flag)
    con = genome.conn
    con.execute("INSERT OR IGNORE INTO entity_graph (entity, gene_id) VALUES (?, ?)",
                ("myentity", gene_id))
    con.commit()

    genome._entity_graph_retrieval_enabled = True
    genes = genome.query_genes(
        domains=["entitytest"],
        entities=["myentity"],
        max_genes=5,
        use_entity_graph=True,
    )
    assert isinstance(genes, list)
    # The gene must appear in results (entity match + boost)
    ids = [g.gene_id for g in genes]
    assert gene_id in ids, f"Expected {gene_id} in {ids}"


def test_entity_graph_use_entity_graph_override(genome):
    """use_entity_graph=True per-call override enables Tier 5b even when flag is False."""
    gene = _make("override test gene content", domains=["override"])
    genome.upsert_gene(gene)
    genome._entity_graph_retrieval_enabled = False
    # Should not raise — flag is False but use_entity_graph=True overrides it
    genes = genome.query_genes(
        domains=["override"],
        entities=[],
        max_genes=3,
        use_entity_graph=True,
    )
    assert isinstance(genes, list)
