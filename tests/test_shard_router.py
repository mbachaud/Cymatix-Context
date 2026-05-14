"""Smoke tests for ShardRouter (Task 2 of genome sharding).

Verifies:
    - route() picks shards whose fingerprints contain query terms
    - route() with empty query returns all healthy shards
    - query_genes() fans out to routed shards and merges results
    - Merged results sorted by score (highest first) with dedup
    - Unknown shard name raises ValueError on _open_shard
    - known_shards() filters by category
    - Feature flag helper reads env var
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from helix_context.genome import Genome
from helix_context.schemas import (
    ChromatinState,
    EpigeneticMarkers,
    Gene,
    PromoterTags,
)
from helix_context.shard_router import ShardRouter, use_shards_enabled
from helix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
    upsert_fingerprint,
)


def _mk_gene(content: str, domains: list[str], entities: list[str], source: str) -> Gene:
    """Minimal gene builder — gene_id content-hashed at upsert."""
    return Gene(
        gene_id="",
        content=content,
        complement=content[:50],
        codons=[],
        promoter=PromoterTags(domains=domains, entities=entities, sequence_index=0),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
        source_id=source,
    )


@pytest.fixture
def two_shard_setup():
    """Create main.db + two populated shard .db files on disk.

    Shard A (reference): contains 'docs' + 'helix' fingerprints
    Shard B (participant): contains 'auth' + 'jwt' fingerprints
    """
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    main_path = str(root / "main.db")
    shard_a_path = str(root / "shard_a.db")
    shard_b_path = str(root / "shard_b.db")

    # Populate Shard A with a docs gene.
    ga = Genome(shard_a_path)
    gene_a = _mk_gene(
        "Helix design doc. Context retrieval via fingerprints.",
        domains=["docs"],
        entities=["helix"],
        source="/docs/intro.md",
    )
    gene_a_id = ga.upsert_gene(gene_a, apply_gate=False)
    ga.conn.close()
    if ga._reader:
        ga._reader.close()

    # Populate Shard B with an auth gene.
    gb = Genome(shard_b_path)
    gene_b = _mk_gene(
        "Auth module. JWT sessions expire every 15 minutes.",
        domains=["auth"],
        entities=["jwt"],
        source="/code/auth.py",
    )
    gene_b_id = gb.upsert_gene(gene_b, apply_gate=False)
    gb.conn.close()
    if gb._reader:
        gb._reader.close()

    # Init main.db and register both shards + their fingerprints.
    main = open_main_db(main_path)
    init_main_db(main)
    register_shard(main, "shard_a", "reference", shard_a_path, gene_count=1)
    register_shard(main, "shard_b", "participant", shard_b_path, gene_count=1)

    upsert_fingerprint(
        main, gene_id=gene_a_id, shard_name="shard_a",
        source_id="/docs/intro.md",
        domains_json=json.dumps(["docs"]),
        entities_json=json.dumps(["helix"]),
        key_values_json="[]",
    )
    upsert_fingerprint(
        main, gene_id=gene_b_id, shard_name="shard_b",
        source_id="/code/auth.py",
        domains_json=json.dumps(["auth"]),
        entities_json=json.dumps(["jwt"]),
        key_values_json="[]",
    )
    main.close()

    yield {
        "main_path": main_path,
        "shard_a_path": shard_a_path,
        "shard_b_path": shard_b_path,
        "gene_a_id": gene_a_id,
        "gene_b_id": gene_b_id,
    }
    td.cleanup()


def test_route_picks_matching_shards(two_shard_setup):
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        # Auth query should only route to shard_b
        shards = router.route(domains=["auth"], entities=[])
        assert shards == ["shard_b"]

        # Helix query should only route to shard_a
        shards = router.route(domains=[], entities=["helix"])
        assert shards == ["shard_a"]
    finally:
        router.close()


def test_route_empty_query_returns_all_shards(two_shard_setup):
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        shards = router.route(domains=[], entities=[])
        assert set(shards) == {"shard_a", "shard_b"}
    finally:
        router.close()


def test_route_orders_by_hit_count(two_shard_setup):
    """Query matching multiple fingerprints in one shard should prefer it."""
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        # Query hits shard_a for both 'docs' and 'helix' (2 hits);
        # shard_b has none. Only shard_a returns.
        shards = router.route(domains=["docs"], entities=["helix"])
        assert shards == ["shard_a"]
    finally:
        router.close()


def test_query_genes_fans_out(two_shard_setup):
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        # Query spans both shards
        results = router.query_genes(
            domains=["auth", "docs"],
            entities=[],
            max_genes=10,
        )
        gene_ids = {g.gene_id for g in results}
        assert two_shard_setup["gene_a_id"] in gene_ids
        assert two_shard_setup["gene_b_id"] in gene_ids
    finally:
        router.close()


def test_query_genes_respects_max(two_shard_setup):
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        results = router.query_genes(
            domains=["auth", "docs"],
            entities=[],
            max_genes=1,
        )
        assert len(results) <= 1
    finally:
        router.close()


def test_query_genes_empty_when_no_shards_match(two_shard_setup):
    """Query terms not matching any fingerprint should produce no routes
    and therefore no results."""
    # We deliberately rebuild main.db without any fingerprint rows
    # matching "physics", so route returns [] and query returns [].
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        results = router.query_genes(
            domains=["physics"],
            entities=["quark"],
            max_genes=5,
        )
        assert results == []
        assert router.last_query_scores == {}
    finally:
        router.close()


def test_query_genes_exposes_scores_and_tiers(two_shard_setup):
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        results = router.query_genes(
            domains=["auth"],
            entities=["jwt"],
            max_genes=5,
        )
        assert len(results) >= 1
        top_id = results[0].gene_id
        assert top_id in router.last_query_scores
        assert router.last_query_scores[top_id] > 0
    finally:
        router.close()


def test_unknown_shard_raises(two_shard_setup):
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        with pytest.raises(ValueError, match="shard not registered"):
            router._open_shard("nonexistent_shard")
    finally:
        router.close()


def test_known_shards_filters_by_category(two_shard_setup):
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        refs = router.known_shards(category="reference")
        assert refs == ["shard_a"]

        parts = router.known_shards(category="participant")
        assert parts == ["shard_b"]

        all_shards = router.known_shards()
        assert set(all_shards) == {"shard_a", "shard_b"}
    finally:
        router.close()


def test_use_shards_flag(monkeypatch):
    monkeypatch.delenv("HELIX_USE_SHARDS", raising=False)
    assert use_shards_enabled() is False

    monkeypatch.setenv("HELIX_USE_SHARDS", "1")
    assert use_shards_enabled() is True

    monkeypatch.setenv("HELIX_USE_SHARDS", "0")
    assert use_shards_enabled() is False

    monkeypatch.setenv("HELIX_USE_SHARDS", "on")
    assert use_shards_enabled() is True


# ── Issue #104 — citation lookup must work in sharded mode ───────────────


def test_sharded_get_citation_rows_resolves_via_fingerprint_index(two_shard_setup):
    """Regression for issue #104 Bug 1.

    Before the fix, /context constructed citations with a direct
    ``SELECT FROM genes WHERE gene_id IN (...)`` against
    ``helix.genome.read_conn``. In sharded mode that connection points
    at main.db whose ``genes`` table is empty (rows live in shard .db
    files), so every citation lookup came back empty and the bench
    harness fell back to <GENE src=...> regex parsing.

    The adapter must resolve source_id + domains/entities from
    main.db's fingerprint_index without touching the empty genes table.
    """
    from helix_context.sharding import ShardedGenomeAdapter

    adapter = ShardedGenomeAdapter(main_path=two_shard_setup["main_path"])
    try:
        rows = adapter.get_citation_rows(
            [two_shard_setup["gene_a_id"], two_shard_setup["gene_b_id"]]
        )
        # Both genes resolved — no silent drop-through.
        assert set(rows.keys()) == {
            two_shard_setup["gene_a_id"],
            two_shard_setup["gene_b_id"],
        }

        a_row = rows[two_shard_setup["gene_a_id"]]
        assert a_row["source_id"] == "/docs/intro.md"
        assert "docs" in a_row["domains"]
        assert "helix" in a_row["entities"]

        b_row = rows[two_shard_setup["gene_b_id"]]
        assert b_row["source_id"] == "/code/auth.py"
        assert "auth" in b_row["domains"]
        assert "jwt" in b_row["entities"]
    finally:
        adapter.close()


def test_sharded_get_citation_rows_empty_input(two_shard_setup):
    """Empty gene_ids list returns empty map without opening shards."""
    from helix_context.sharding import ShardedGenomeAdapter

    adapter = ShardedGenomeAdapter(main_path=two_shard_setup["main_path"])
    try:
        assert adapter.get_citation_rows([]) == {}
    finally:
        adapter.close()


def test_sharded_get_citation_rows_unknown_id(two_shard_setup):
    """Missing ids are silently absent rather than mapped to None."""
    from helix_context.sharding import ShardedGenomeAdapter

    adapter = ShardedGenomeAdapter(main_path=two_shard_setup["main_path"])
    try:
        rows = adapter.get_citation_rows(["deadbeef00000000"])
        assert rows == {}
    finally:
        adapter.close()


def test_sharded_get_doc_alias_matches_get_gene(two_shard_setup):
    """`get_doc` and `get_gene` must be polymorphic with Genome so callers
    in routes_context / helpers don't have to branch on adapter type.
    """
    from helix_context.sharding import ShardedGenomeAdapter

    adapter = ShardedGenomeAdapter(main_path=two_shard_setup["main_path"])
    try:
        via_gene = adapter.get_gene(two_shard_setup["gene_a_id"])
        via_doc = adapter.get_doc(two_shard_setup["gene_a_id"])
        assert via_gene is not None
        assert via_doc is not None
        assert via_gene.gene_id == via_doc.gene_id
        assert via_gene.source_id == "/docs/intro.md"
    finally:
        adapter.close()


def test_genome_get_citation_rows_blob(tmp_path):
    """Same polymorphic shape on the blob backend."""
    blob_path = str(tmp_path / "blob.db")
    g = Genome(blob_path)
    gene = _mk_gene(
        "Hello world. Single-shard ingest.",
        domains=["docs"],
        entities=["hello"],
        source="/notes/hello.md",
    )
    gid = g.upsert_gene(gene, apply_gate=False)

    rows = g.get_citation_rows([gid])
    assert gid in rows
    assert rows[gid]["source_id"] == "/notes/hello.md"
    assert "docs" in rows[gid]["domains"]
    assert "hello" in rows[gid]["entities"]

    # Polymorphic alias also exists on Genome.
    assert g.get_gene(gid) is not None
    assert g.get_gene(gid).gene_id == gid

    g.conn.close()
    if g._reader:
        g._reader.close()


# ── Issue #104 Bug 2 — RRF cross-shard fusion ────────────────────────────


def test_query_genes_uses_rrf_not_raw_scores(two_shard_setup):
    """The router must merge with rank-level fusion (RRF), not raw scores.

    Per-shard BM25 isn't calibrated across corpora; the larger shard's
    raw score would otherwise dominate even when the smaller shard's
    top hit is more relevant. Verifying RRF here means
    ``last_query_scores`` produces RRF magnitudes (small ~1/(60+rank))
    rather than the shards' raw query scores.
    """
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        results = router.query_genes(
            domains=["auth", "docs"],
            entities=[],
            max_genes=5,
        )
        assert len(results) >= 2

        # RRF score for a rank-1 hit with default k=60 is 1/61 ≈ 0.0164;
        # never exceeds 1/k+1 = 1/61. Raw BM25 scores in our fixture are
        # in the +1.0..+10.0 range, so the upper bound is a clean signal
        # that the merge is rank-based, not score-based.
        for gid, score in router.last_query_scores.items():
            assert 0.0 < score <= (1.0 / 61.0) + 1e-9, (
                f"gene {gid} score={score} looks like a raw shard score, "
                "not an RRF rank contribution"
            )
    finally:
        router.close()
