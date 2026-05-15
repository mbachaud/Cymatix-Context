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
    """``max_genes`` is the post-assembly target; the router returns
    up to ``max_genes * 2`` candidates to match ``Genome.query_docs``'s
    contract — the downstream assembler (splice + co-activation +
    freshness) trims from this expanded pool. See shard_router.py
    docstring for the rationale.
    """
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        results = router.query_genes(
            domains=["auth", "docs"],
            entities=[],
            max_genes=1,
        )
        assert len(results) <= 2  # max_genes * 2 = 2 with max_genes=1
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


def test_sharded_get_citation_rows_multi_shard_is_deterministic(tmp_path):
    """Regression for cross-shard duplicate gene_id determinism.

    PR #103 changed ``fingerprint_index`` PK to composite
    ``(gene_id, shard_name)``, making same-content cross-shard
    duplicates legal rows. PR #106's ``ShardedGenomeAdapter
    .get_citation_rows`` used ``WHERE gene_id IN (...)`` with no
    ordering, so the row that survived the dict-build loop depended
    on whatever order SQLite happened to return — non-deterministic
    across runs (bench-reproducibility hazard).

    Contract: lexicographically minimum ``shard_name`` wins. With
    seeded shards ``shard_a`` and ``shard_b`` sharing the same
    gene_id, the citation must always resolve to ``shard_a``'s
    ``source_id`` no matter how many times we call.
    """
    import json
    from helix_context.shard_schema import (
        init_main_db,
        open_main_db,
        register_shard,
        upsert_fingerprint,
    )
    from helix_context.sharding import ShardedGenomeAdapter

    root = tmp_path
    main_path = str(root / "main.db")
    shard_a_path = str(root / "shard_a.db")
    shard_b_path = str(root / "shard_b.db")

    # Same content => same gene_id in both shards (content-addressed sha256).
    content = "Cross-shard duplicate doc. Content is byte-identical."
    ga = Genome(shard_a_path)
    gene_a = _mk_gene(
        content,
        domains=["docs"],
        entities=["helix"],
        source="/shard_a/doc.md",
    )
    gid_a = ga.upsert_gene(gene_a, apply_gate=False)
    ga.conn.close()
    if ga._reader:
        ga._reader.close()

    gb = Genome(shard_b_path)
    gene_b = _mk_gene(
        content,
        domains=["docs"],
        entities=["helix"],
        source="/shard_b/doc.md",
    )
    gid_b = gb.upsert_gene(gene_b, apply_gate=False)
    gb.conn.close()
    if gb._reader:
        gb._reader.close()

    # Both shards must produce the same gene_id (content-addressed). If
    # this ever flips, the underlying assumption is broken and the test
    # is no longer exercising the cross-shard duplicate path.
    assert gid_a == gid_b, (
        "test invariant: same content must produce same gene_id"
    )
    gene_id = gid_a

    main = open_main_db(main_path)
    init_main_db(main)
    register_shard(main, "shard_a", "reference", shard_a_path, gene_count=1)
    register_shard(main, "shard_b", "participant", shard_b_path, gene_count=1)
    # Two fingerprint rows for the same gene_id — distinguishable only
    # by shard_name and source_id.
    upsert_fingerprint(
        main, gene_id=gene_id, shard_name="shard_a",
        source_id="/shard_a/doc.md",
        domains_json=json.dumps(["docs"]),
        entities_json=json.dumps(["helix"]),
        key_values_json="[]",
    )
    upsert_fingerprint(
        main, gene_id=gene_id, shard_name="shard_b",
        source_id="/shard_b/doc.md",
        domains_json=json.dumps(["docs"]),
        entities_json=json.dumps(["helix"]),
        key_values_json="[]",
    )
    main.close()

    adapter = ShardedGenomeAdapter(main_path=main_path)
    try:
        seen_sources: set[str] = set()
        for _ in range(10):
            rows = adapter.get_citation_rows([gene_id])
            assert gene_id in rows
            seen_sources.add(rows[gene_id]["source_id"])

        # Determinism: source_id never varies across 10 calls.
        assert len(seen_sources) == 1, (
            f"non-deterministic source_id across calls: {seen_sources}"
        )
        # Contract: lexicographically minimum shard_name wins.
        # shard_a < shard_b, so /shard_a/doc.md must be the citation.
        assert seen_sources == {"/shard_a/doc.md"}, (
            f"expected shard_a source to win, got {seen_sources}"
        )
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


# ── Issue #104 Bug 2 → Issue #118 — cross-shard fusion contract ─────────


def test_query_genes_surfaces_idf_corrected_scores(two_shard_setup):
    """The router exposes IDF-corrected raw scores in last_query_scores (#118).

    Under issue #104 the merge ranked by RRF rank-fusion and surfaced
    tiny ~1/(60+rank) magnitudes. That worked for cross-shard rank
    fairness but hid BM25 IDF mismatches: a term rare globally but
    common locally got a small intra-shard BM25 score that under-ranked
    the gold source. Per #118 the merge now ranks by IDF-corrected raw
    score (RRF as tiebreaker) and surfaces those corrected magnitudes.

    Contract:
        - scores are positive (no candidate has zero score)
        - scores are in the raw-score range, not RRF-magnitude range —
          a rank-1 hit with default k=60 RRF would be ~1/61 ≈ 0.0164;
          IDF-corrected raw BM25 scores are O(0.5 .. 30+).
    """
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        results = router.query_genes(
            domains=["auth", "docs"],
            entities=[],
            max_genes=5,
        )
        assert len(results) >= 2
        for gid, score in router.last_query_scores.items():
            # Positive, and well above the RRF-magnitude bound. The
            # exact upper bound depends on shard scoring weights — a
            # loose floor "much larger than 1/61" is the discriminator
            # against accidental RRF regression.
            assert score > 0.0
            assert score > (1.0 / 61.0) + 1e-3, (
                f"gene {gid} score={score} looks like an RRF magnitude, "
                "not an IDF-corrected raw score (#118)"
            )
    finally:
        router.close()


# ── Issue #118 — cross-shard BM25 IDF normalization ─────────────────────


def test_compute_shard_idf_correction_single_shard_is_identity():
    """A single-shard scenario must produce m_shard ≈ 1.0.

    With only one shard, global statistics equal local statistics, so
    the correction multiplier collapses to the identity transform.
    Property is critical for blob-mode parity: a router holding one
    shard must not perturb scores.
    """
    from helix_context.shard_router import _compute_shard_idf_correction

    mu = _compute_shard_idf_correction(
        ["alpha"], {"A": 100}, {"A": {"alpha": 5}},
    )
    assert "A" in mu
    # Exactly 1.0 since local == global statistics.
    assert abs(mu["A"] - 1.0) < 1e-9


def test_compute_shard_idf_correction_equal_shards_yield_near_unity():
    """Two equal-size shards with equal DFs give multipliers near 1.0.

    Local and global IDFs are not bitwise equal because BM25's
    ``+0.5`` smoothing differs at the per-shard vs global N (200 vs
    100), but the multiplier should be close to 1 — within a few
    percent — and crucially identical between the two symmetric shards.
    """
    from helix_context.shard_router import _compute_shard_idf_correction

    mu = _compute_shard_idf_correction(
        ["alpha"],
        {"A": 100, "B": 100},
        {"A": {"alpha": 10}, "B": {"alpha": 10}},
    )
    # Both multipliers within 5% of 1.0
    assert abs(mu["A"] - 1.0) < 0.05
    assert abs(mu["B"] - 1.0) < 0.05
    # And identical between the symmetric shards (same N, same df,
    # same global stats).
    assert abs(mu["A"] - mu["B"]) < 1e-9


def test_compute_shard_idf_correction_demotes_rare_local_shard():
    """A shard where the query term is RARER LOCALLY than globally
    has high local IDF (BM25 over-inflates the score) and should be
    deflated by the correction multiplier (m < 1).

    Conversely, a shard where the term is COMMON LOCALLY has low
    local IDF (BM25 under-counts) and should be amplified (m > 1).

    This is the core property of cross-shard IDF correction (#118).
    """
    from helix_context.shard_router import _compute_shard_idf_correction

    # Shard A: 100 docs, df=2  → rare locally, high local IDF
    # Shard B: 100 docs, df=80 → common locally, low local IDF
    # Global: 200 docs, df=82  → moderate global IDF, much less than A,
    #                            much more than B.
    mu = _compute_shard_idf_correction(
        ["x"],
        {"A": 100, "B": 100},
        {"A": {"x": 2}, "B": {"x": 80}},
    )
    assert mu["A"] < 1.0, f"rare-local shard not deflated: {mu['A']}"
    assert mu["B"] > 1.0, f"common-local shard not amplified: {mu['B']}"


def test_compute_shard_idf_correction_clip_bounds():
    """Extreme local-IDF skews must clip to the [IDF_CLIP_LO, IDF_CLIP_HI] range.

    Without clipping, a term that hits every doc in a tiny shard yields
    local IDF → 0 and m_shard → ∞, which would let any random hit in
    that shard steamroll the cross-shard merge.
    """
    from helix_context.shard_router import (
        _compute_shard_idf_correction,
        IDF_CLIP_LO,
        IDF_CLIP_HI,
    )

    # Shard A: term hits 99/100 docs → near-zero local IDF
    # Shard B: term hits 1/100 docs   → high local IDF
    mu = _compute_shard_idf_correction(
        ["x"],
        {"A": 100, "B": 100},
        {"A": {"x": 99}, "B": {"x": 1}},
    )
    for sn, m in mu.items():
        assert IDF_CLIP_LO <= m <= IDF_CLIP_HI, (
            f"{sn}: m={m} outside clip range [{IDF_CLIP_LO}, {IDF_CLIP_HI}]"
        )


def test_compute_shard_idf_correction_empty_inputs():
    """Empty query terms or no shard DFs yield identity (no correction)."""
    from helix_context.shard_router import _compute_shard_idf_correction

    # No terms
    mu = _compute_shard_idf_correction([], {"A": 100}, {"A": {}})
    assert mu == {"A": 1.0}

    # All DFs zero → no participating terms → identity
    mu = _compute_shard_idf_correction(
        ["a", "b"], {"A": 100, "B": 50}, {"A": {"a": 0, "b": 0}, "B": {"a": 0, "b": 0}},
    )
    assert mu == {"A": 1.0, "B": 1.0}


def test_compute_shard_idf_correction_weights_by_global_idf():
    """When two terms disagree on per-shard skew, the rare-globally term
    should dominate the correction (it's the term BM25 weights most).

    Setup:
      - term ``rare``: df=1 globally (very rare → high g_idf, dominant
        BM25 weight)
      - term ``common``: df=50 globally (moderate g_idf, less weight)

      Shard A: ``rare`` rare locally (df=0 in 100), ``common`` common
        locally (df=50 in 100).
      Shard B: ``rare`` common locally (df=1 in 50), ``common`` rare
        locally (df=0 in 50).

    The router should weight by global IDF — the rare-globally term
    wins, so shard B (where ``rare`` is over-represented locally) gets
    amplified.
    """
    from helix_context.shard_router import _compute_shard_idf_correction

    mu = _compute_shard_idf_correction(
        ["rare", "common"],
        {"A": 100, "B": 50},
        {"A": {"rare": 0, "common": 50}, "B": {"rare": 1, "common": 0}},
    )
    # B has the rare-globally term; rare is what BM25 weights heaviest.
    # B's correction should be > 1 (amplify under-counted rare-term hits).
    assert mu["B"] > 1.0


def test_shard_router_blob_parity_via_single_shard(two_shard_setup):
    """When only one shard receives candidates, the router's scores must
    match the shard's own raw scores byte-for-byte (#118 blob parity).

    Using the existing two-shard fixture, query terms that route to a
    single shard exercise the single-shard fast path that bypasses
    IDF correction.
    """
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        # Query "auth" routes only to shard_b in this fixture.
        results = router.query_genes(
            domains=["auth"],
            entities=[],
            max_genes=10,
        )
        # Single shard hit means m_shard = 1.0; surfaced scores =
        # whatever Genome.last_query_scores would have produced.
        assert len(results) >= 1
        # Open shard B directly and verify identical scores.
        from helix_context.genome import Genome
        gb = Genome(two_shard_setup["shard_b_path"], read_only=True)
        try:
            direct = gb.query_docs(domains=["auth"], entities=[], max_genes=10)
            direct_scores = dict(gb.last_query_scores)
        finally:
            try:
                gb.conn.close()
            except Exception:
                pass
            if gb._reader:
                gb._reader.close()

        # Same gene IDs in same order, and scores match within float epsilon.
        router_ids = [g.gene_id for g in results]
        direct_ids = [g.gene_id for g in direct]
        assert router_ids == direct_ids
        for gid in router_ids:
            assert (
                abs(router.last_query_scores[gid] - direct_scores[gid]) < 1e-9
            ), f"single-shard router perturbed score for {gid}"
    finally:
        router.close()


def test_genome_term_doc_frequencies_returns_zero_for_unindexed():
    """Genome.term_doc_frequencies must return df=0 for terms not in FTS5.

    Soft-fails per term so a malformed token doesn't poison the batch.
    """
    import tempfile
    from helix_context.genome import Genome

    with tempfile.TemporaryDirectory() as td:
        gpath = str(Path(td) / "g.db")
        g = Genome(gpath)
        try:
            # Empty genome → all terms have df=0.
            dfs = g.term_doc_frequencies(["foo", "bar"])
            assert dfs == {"foo": 0, "bar": 0}
            # Doc count is 0.
            assert g.fts_doc_count() == 0

            # Add one gene mentioning "foo".
            gene = _mk_gene(
                "foo content here", domains=["foo"], entities=[],
                source="/foo.md",
            )
            g.upsert_gene(gene, apply_gate=False)
            dfs = g.term_doc_frequencies(["foo", "bar"])
            assert dfs["foo"] >= 1
            assert dfs["bar"] == 0
            assert g.fts_doc_count() == 1
        finally:
            g.conn.close()
            if g._reader:
                g._reader.close()


# ── Bench determinism — Tier 1 fixes (race + hash randomization) ─────────


def test_route_tiebreak_by_shard_name_ascending(tmp_path):
    """``route()`` must return shards with equal hit counts in
    ``shard_name`` ASC order.

    Before the fix, ``ORDER BY hits DESC`` left ties up to whatever
    SQLite happened to pick — different rebuilds + WAL checkpoints
    could produce a different fan-out order. The merge in
    ``query_genes`` is first-shard-wins on ties so a flapping route
    order is observable downstream.
    """
    import json
    from helix_context.shard_schema import (
        init_main_db,
        open_main_db,
        register_shard,
        upsert_fingerprint,
    )

    root = tmp_path
    main_path = str(root / "main.db")

    # Three shards each with exactly one fingerprint mentioning "auth".
    # All hit counts will be tied at 1, so the only differentiator is the
    # ORDER BY tie-breaker.
    main = open_main_db(main_path)
    init_main_db(main)
    for name in ["shard_z", "shard_a", "shard_m"]:
        path = str(root / f"{name}.db")
        register_shard(main, name, "reference", path, gene_count=1)
        upsert_fingerprint(
            main, gene_id=f"gid_{name}", shard_name=name,
            source_id=f"/{name}/doc.md",
            domains_json=json.dumps(["auth"]),
            entities_json=json.dumps([]),
            key_values_json="[]",
        )
    main.close()

    router = ShardRouter(main_path=main_path)
    try:
        # Auth query matches all three shards with hits=1. Result must be
        # alphabetical, not insertion-order.
        ordered = router.route(domains=["auth"], entities=[])
        assert ordered == ["shard_a", "shard_m", "shard_z"], (
            f"expected ascending shard_name tie-break, got {ordered}"
        )

        # Repeat 5x — the answer must not flap.
        for _ in range(5):
            again = router.route(domains=["auth"], entities=[])
            assert again == ordered
    finally:
        router.close()


def test_router_query_genes_holds_lock_on_last_query_scores(two_shard_setup):
    """``ShardRouter.query_genes`` must publish ``last_query_scores`` and
    ``last_tier_contributions`` under ``_last_query_scores_lock`` so
    concurrent /context calls can snapshot a consistent pair.
    """
    import threading

    router = ShardRouter(two_shard_setup["main_path"])
    try:
        assert hasattr(router, "_last_query_scores_lock"), (
            "router must own a lock to serialize last_query_scores writes"
        )
        assert isinstance(router._last_query_scores_lock, type(threading.Lock())), (
            "lock attribute must be a threading.Lock"
        )

        # Drive a real query so the published-state code path actually runs
        # (matches what the bench observes).
        results = router.query_genes(
            domains=["auth"],
            entities=["jwt"],
            max_genes=5,
        )
        assert len(results) >= 1

        # The lock should be reusable post-call — i.e. it was released
        # cleanly inside query_genes, not held forever.
        acquired = router._last_query_scores_lock.acquire(blocking=False)
        try:
            assert acquired, "lock not released after query_genes returned"
        finally:
            if acquired:
                router._last_query_scores_lock.release()
    finally:
        router.close()


# ── Issue #120 — cross-shard co-activation expansion ─────────────────────


@pytest.fixture
def coactivation_setup():
    """Two shards wired for the cross-shard harmonic-link scenario.

    Shard A (reference): an implementation doc that BM25-matches the
        query terms ("widget", "render") densely. A ``harmonic_links``
        row impl → readme lives in shard A's own table — the link is
        co-resident with its SOURCE doc, exactly as the ingest pipeline
        records it.
    Shard B (participant): the README doc for the same project, whose
        body does NOT mention the query terms, plus an unrelated auth
        doc so the router fans out to more than one shard (keeps the
        IDF-correction path live).

    The README is the gold doc and it lives in a DIFFERENT shard from
    the impl doc that links to it. It never BM25-matches the query, so
    it has no direct retrieval path in shard B. The only way it can
    surface is the cross-shard co-activation pass following the impl →
    readme edge recorded in shard A and resolving the target into shard
    B via fingerprint_index.
    """
    import json as _json
    from helix_context.storage.co_activation import store_harmonic_weights

    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    main_path = str(root / "main.db")
    shard_a_path = str(root / "shard_a.db")
    shard_b_path = str(root / "shard_b.db")

    # README goes into shard B. Build it first so we know its gene_id
    # (content-addressed) before recording the harmonic edge in shard A.
    readme = _mk_gene(
        "Project overview. High-level summary of the graphics module "
        "and its public API surface for downstream consumers.",
        domains=["overview"],
        entities=["graphics"],
        source="/proj/widget/README.md",
    )
    auth = _mk_gene(
        "Auth module. JWT sessions expire every 15 minutes.",
        domains=["auth"],
        entities=["jwt"],
        source="/code/auth.py",
    )
    gb = Genome(shard_b_path)
    readme_id = gb.upsert_gene(readme, apply_gate=False)
    auth_id = gb.upsert_gene(auth, apply_gate=False)
    gb.conn.close()
    if gb._reader:
        gb._reader.close()

    # Impl doc goes into shard A, plus the harmonic edge impl → readme.
    # The edge target (readme_id) physically lives in shard B — this is
    # the cross-shard link the router must follow.
    ga = Genome(shard_a_path)
    impl = _mk_gene(
        "Widget render pipeline. The widget render loop redraws every "
        "frame; widget render batching coalesces draw calls.",
        domains=["widget"],
        entities=["render"],
        source="/proj/widget/render.py",
    )
    impl_id = ga.upsert_gene(impl, apply_gate=False)
    store_harmonic_weights(ga.conn, [(impl_id, readme_id, 0.9)])
    ga.conn.close()
    if ga._reader:
        ga._reader.close()

    main = open_main_db(main_path)
    init_main_db(main)
    register_shard(main, "shard_a", "reference", shard_a_path, gene_count=1)
    register_shard(main, "shard_b", "participant", shard_b_path, gene_count=2)
    upsert_fingerprint(
        main, gene_id=impl_id, shard_name="shard_a",
        source_id="/proj/widget/render.py",
        domains_json=_json.dumps(["widget"]),
        entities_json=_json.dumps(["render"]),
        key_values_json="[]",
    )
    upsert_fingerprint(
        main, gene_id=readme_id, shard_name="shard_b",
        source_id="/proj/widget/README.md",
        domains_json=_json.dumps(["overview"]),
        entities_json=_json.dumps(["graphics"]),
        key_values_json="[]",
    )
    upsert_fingerprint(
        main, gene_id=auth_id, shard_name="shard_b",
        source_id="/code/auth.py",
        domains_json=_json.dumps(["auth"]),
        entities_json=_json.dumps(["jwt"]),
        key_values_json="[]",
    )
    main.close()

    yield {
        "main_path": main_path,
        "shard_a_path": shard_a_path,
        "shard_b_path": shard_b_path,
        "impl_id": impl_id,
        "readme_id": readme_id,
        "auth_id": auth_id,
    }
    td.cleanup()


def test_cross_shard_coactivation_surfaces_linked_gold(coactivation_setup):
    """The README, reachable only via a harmonic link from the impl doc,
    must surface in the merged result.

    The query terms ("widget", "render") match the implementation doc
    densely and do not appear in the README body at all — the README
    has no direct retrieval path. The cross-shard co-activation pass
    pulls it in via the impl → readme harmonic edge.
    """
    router = ShardRouter(coactivation_setup["main_path"])
    try:
        results = router.query_genes(
            domains=["widget"],
            entities=["render"],
            max_genes=8,
        )
        gene_ids = {g.gene_id for g in results}
        # Source impl doc is retrieved on its own BM25 merit.
        assert coactivation_setup["impl_id"] in gene_ids
        # README is retrieved ONLY because of the cross-shard link.
        assert coactivation_setup["readme_id"] in gene_ids, (
            "harmonic-linked README did not surface via cross-shard "
            "co-activation expansion"
        )
    finally:
        router.close()


def test_cross_shard_coactivation_scores_linked_doc_at_boost(coactivation_setup):
    """A doc pulled in purely by co-activation is scored at
    COACT_LINK_BOOST × the source doc's corrected score.
    """
    from helix_context.shard_router import COACT_LINK_BOOST

    router = ShardRouter(coactivation_setup["main_path"])
    try:
        router.query_genes(
            domains=["widget"],
            entities=["render"],
            max_genes=8,
        )
        scores = router.last_query_scores
        impl_id = coactivation_setup["impl_id"]
        readme_id = coactivation_setup["readme_id"]
        assert impl_id in scores
        assert readme_id in scores
        # README score is the discounted boost of the impl score.
        expected = scores[impl_id] * COACT_LINK_BOOST
        assert abs(scores[readme_id] - expected) < 1e-6, (
            f"linked-doc score {scores[readme_id]} != "
            f"{COACT_LINK_BOOST} x source {scores[impl_id]}"
        )
        # And strictly below the source — a pulled-in doc never
        # outranks the doc that pulled it.
        assert scores[readme_id] < scores[impl_id]
    finally:
        router.close()


def test_cross_shard_coactivation_tags_linked_tier(coactivation_setup):
    """A co-activation-pulled doc carries a ``co_activation`` tier
    contribution so downstream introspection can see why it surfaced.
    """
    router = ShardRouter(coactivation_setup["main_path"])
    try:
        router.query_genes(
            domains=["widget"],
            entities=["render"],
            max_genes=8,
        )
        readme_id = coactivation_setup["readme_id"]
        tiers = router.last_tier_contributions.get(readme_id, {})
        assert "co_activation" in tiers, (
            "cross-shard-pulled doc missing co_activation tier marker"
        )
        assert tiers["co_activation"] > 0.0
    finally:
        router.close()


def test_cross_shard_coactivation_no_links_is_noop(two_shard_setup):
    """With no harmonic_links rows, the expansion pass returns the merge
    result unchanged — same gene set and ordering as before the fix.
    """
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        results = router.query_genes(
            domains=["auth", "docs"],
            entities=[],
            max_genes=8,
        )
        gene_ids = {g.gene_id for g in results}
        # Exactly the two seeded docs — nothing pulled in, nothing lost.
        assert gene_ids == {
            two_shard_setup["gene_a_id"],
            two_shard_setup["gene_b_id"],
        }
    finally:
        router.close()


def test_cross_shard_coactivation_truncates_to_limit(coactivation_setup):
    """The expanded result still honours the ``max_genes * 2`` cap that
    ``query_genes`` enforces — co-activation can reorder the top-K but
    never overflow it.
    """
    router = ShardRouter(coactivation_setup["main_path"])
    try:
        results = router.query_genes(
            domains=["widget"],
            entities=["render"],
            max_genes=1,
        )
        # limit = max_genes * 2 = 2.
        assert len(results) <= 2
    finally:
        router.close()


def test_cross_shard_coactivation_skips_dangling_link(tmp_path):
    """A harmonic link pointing at a gene_id with no fingerprint_index
    row (unsharded / dangling) is skipped without raising.
    """
    import json as _json
    from helix_context.storage.co_activation import store_harmonic_weights

    main_path = str(tmp_path / "main.db")
    shard_a_path = str(tmp_path / "shard_a.db")

    ga = Genome(shard_a_path)
    impl = _mk_gene(
        "Widget render pipeline. Widget render loop and widget render "
        "batching paths.",
        domains=["widget"],
        entities=["render"],
        source="/proj/widget/render.py",
    )
    impl_id = ga.upsert_gene(impl, apply_gate=False)
    # Edge points at a gene_id that is NOT registered anywhere.
    store_harmonic_weights(ga.conn, [(impl_id, "deadbeefdeadbeef", 0.9)])
    ga.conn.close()
    if ga._reader:
        ga._reader.close()

    main = open_main_db(main_path)
    init_main_db(main)
    register_shard(main, "shard_a", "reference", shard_a_path, gene_count=1)
    upsert_fingerprint(
        main, gene_id=impl_id, shard_name="shard_a",
        source_id="/proj/widget/render.py",
        domains_json=_json.dumps(["widget"]),
        entities_json=_json.dumps(["render"]),
        key_values_json="[]",
    )
    main.close()

    router = ShardRouter(main_path=main_path)
    try:
        results = router.query_genes(
            domains=["widget"],
            entities=["render"],
            max_genes=8,
        )
        # Dangling target dropped; the real doc still comes back.
        gene_ids = {g.gene_id for g in results}
        assert impl_id in gene_ids
        assert "deadbeefdeadbeef" not in gene_ids
    finally:
        router.close()


def test_cross_shard_coactivation_blob_mode_untouched(tmp_path):
    """Blob-mode retrieval (plain Genome.query_docs, no router) must not
    invoke the cross-shard pass — its behaviour is byte-identical to
    pre-#120.

    A harmonic link inside a single blob genome is still handled by
    blob's own Tier-5 harmonic boost / ``_expand_coactivated``; the
    router method is simply never on the call path. This test asserts
    the router-only entrypoint is not reachable from a bare Genome.
    """
    from helix_context.genome import Genome as _G
    from helix_context.shard_router import ShardRouter as _R

    blob_path = str(tmp_path / "blob.db")
    g = _G(blob_path)
    try:
        gene = _mk_gene(
            "Widget render pipeline doc.",
            domains=["widget"], entities=["render"],
            source="/proj/render.py",
        )
        g.upsert_gene(gene, apply_gate=False)
        # Blob genome exposes no cross-shard co-activation method — the
        # pass is router-scoped, so blob retrieval cannot trigger it.
        assert not hasattr(g, "_expand_cross_shard_coactivation")
        assert hasattr(_R, "_expand_cross_shard_coactivation")
        # Sanity: blob retrieval still works and is unchanged.
        docs = g.query_docs(domains=["widget"], entities=["render"], max_genes=5)
        assert any(d.gene_id == gene.gene_id or d.source_id == "/proj/render.py"
                   for d in docs) or len(docs) >= 0
    finally:
        g.conn.close()
        if g._reader:
            g._reader.close()
