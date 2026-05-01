"""Tests for bounded BM25 lexical rescue."""

from helix_context.genome import Genome
from helix_context.lexical_rescue import (
    lexical_rescue_sources,
    merge_source_ids,
)

from tests.conftest import make_gene


def test_merge_source_ids_preserves_packet_order_and_dedupes():
    merged = merge_source_ids(
        ["F:/repo/a.py", "F:/repo/b.py"],
        ["f:/repo/a.py", "F:/repo/c.py"],
        max_sources=3,
    )

    assert merged == ["F:/repo/a.py", "F:/repo/b.py", "F:/repo/c.py"]


def test_lexical_rescue_finds_claims_for_singular_query(tmp_path):
    db = tmp_path / "genome.db"
    genome = Genome(str(db))
    try:
        gene = make_gene(
            "CLAIM_TYPES allowed values include path_value and config_value.",
            domains=["claims"],
        )
        gene.source_id = "F:/Projects/helix-context/helix_context/schemas.py"
        genome.upsert_gene(gene, apply_gate=False)
    finally:
        genome.close()

    sources = lexical_rescue_sources(
        "claim_type allowed value",
        genome_path=str(db),
        limit=4,
    )

    assert sources == ["F:/Projects/helix-context/helix_context/schemas.py"]


def test_lexical_rescue_excludes_existing_packet_sources(tmp_path):
    db = tmp_path / "genome.db"
    genome = Genome(str(db))
    try:
        first = make_gene("headroom dashboard listens on port 8787", domains=["headroom"])
        first.source_id = "F:/Projects/helix-context/helix.toml"
        genome.upsert_gene(first, apply_gate=False)

        second = make_gene("headroom supervisor default port is 8787", domains=["headroom"])
        second.source_id = "F:/Projects/helix-context/helix_context/launcher/headroom_supervisor.py"
        genome.upsert_gene(second, apply_gate=False)
    finally:
        genome.close()

    sources = lexical_rescue_sources(
        "headroom ports",
        genome_path=str(db),
        limit=4,
        exclude_source_ids=["f:/projects/helix-context/helix.toml"],
    )

    assert sources == [
        "F:/Projects/helix-context/helix_context/launcher/headroom_supervisor.py"
    ]


def test_lexical_rescue_uses_promoter_tags_before_fts_noise(tmp_path):
    db = tmp_path / "genome.db"
    genome = Genome(str(db))
    try:
        noisy = make_gene(
            "A long discussion of ports and listeners and headroom processes.",
            domains=["notes"],
        )
        noisy.source_id = "F:/Projects/helix-context/docs/noisy.md"
        genome.upsert_gene(noisy, apply_gate=False)

        config = make_gene(
            "server configuration values",
            domains=["helix", "headroom", "port"],
        )
        config.source_id = "F:/Projects/helix-context/helix.toml"
        genome.upsert_gene(config, apply_gate=False)
    finally:
        genome.close()

    sources = lexical_rescue_sources(
        "what ports do helix and headroom listen on",
        genome_path=str(db),
        limit=2,
    )

    assert sources[0] == "F:/Projects/helix-context/helix.toml"
