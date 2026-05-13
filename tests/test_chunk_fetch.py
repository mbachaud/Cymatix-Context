"""Tests for chunk-level retrieval helpers."""

from helix_context.encoding.chunk_fetch import fetch_relevant_chunks
from helix_context.genome import Genome

from tests.conftest import make_gene


def test_fetch_relevant_chunks_uses_promoter_tags(tmp_path):
    db = tmp_path / "genome.db"
    genome = Genome(str(db))
    try:
        claim = make_gene(
            "Claim types include path_value and config_value.",
            domains=["claims"],
        )
        claim.source_id = "F:/Projects/helix-context/helix_context/schemas.py"
        genome.upsert_gene(claim, apply_gate=False)

        other = make_gene("Unrelated prose", domains=["notes"])
        other.source_id = "F:/Projects/helix-context/docs/noisy.md"
        genome.upsert_gene(other, apply_gate=False)
    finally:
        genome.close()

    hits = fetch_relevant_chunks(
        "claim_type allowed values",
        genome_path=str(db),
        limit=3,
    )

    assert hits
    assert hits[0].source_id == "F:/Projects/helix-context/helix_context/schemas.py"
    assert "path_value" in hits[0].content


def test_fetch_relevant_chunks_uses_fts_when_promoter_is_sparse(tmp_path):
    db = tmp_path / "genome.db"
    genome = Genome(str(db))
    try:
        gene = make_gene(
            "The headroom dashboard listens on port 8787.",
            domains=["misc"],
        )
        gene.source_id = "F:/Projects/helix-context/helix.toml"
        genome.upsert_gene(gene, apply_gate=False)
    finally:
        genome.close()

    hits = fetch_relevant_chunks(
        "headroom port",
        genome_path=str(db),
        limit=3,
    )

    assert len(hits) == 1
    assert hits[0].source_id == "F:/Projects/helix-context/helix.toml"
    assert "8787" in hits[0].content
