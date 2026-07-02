"""Tests for the cross-shard score-path instrumentation (#181).

Verifies the HELIX_SHARD_SCORE_DEBUG gate on ShardRouter.query_genes:

  (a) flag OFF  -> last_score_breakdown / last_shard_multipliers stay {},
      and the returned ranking is byte-identical to a baseline call (no
      behaviour change at all).
  (b) flag ON   -> last_score_breakdown carries the expected keys for every
      merged candidate, last_shard_multipliers is populated, and for a known
      candidate  corrected == raw * m_shard * doc_type_boost.

Self-contained on-disk 2-shard fixture (mirrors tests/test_shard_router.py's
two_shard_setup) so it runs under `pytest --noconftest`. No GPU, no server,
no network.
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
from helix_context.shard_router import ShardRouter
from helix_context.shard_schema import (
    init_main_db,
    open_main_db,
    register_shard,
    upsert_fingerprint,
)

_DEBUG_ENV = "HELIX_SHARD_SCORE_DEBUG"


def _mk_gene(content: str, domains: list, entities: list, source: str) -> Gene:
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


@pytest.fixture(autouse=True)
def _clear_debug_env():
    """Ensure the debug flag is unset before and after each test."""
    prev = os.environ.pop(_DEBUG_ENV, None)
    try:
        yield
    finally:
        os.environ.pop(_DEBUG_ENV, None)
        if prev is not None:
            os.environ[_DEBUG_ENV] = prev


@pytest.fixture
def two_shard_setup():
    """main.db + two populated shard .db files on disk (2 docs, 2 shards).

    Shard A: a README.md doc tagged docs/helix (eligible for doc-type boost).
    Shard B: an auth.py doc tagged auth/jwt.
    """
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    main_path = str(root / "main.db")
    shard_a_path = str(root / "shard_a.db")
    shard_b_path = str(root / "shard_b.db")

    ga = Genome(shard_a_path)
    gene_a = _mk_gene(
        "Helix design README. Context retrieval via fingerprints and shards.",
        domains=["docs"],
        entities=["helix"],
        source="/docs/README.md",  # basename README.md -> doc-type boost
    )
    gene_a_id = ga.upsert_gene(gene_a, apply_gate=False)
    ga.conn.close()
    if ga._reader:
        ga._reader.close()

    gb = Genome(shard_b_path)
    gene_b = _mk_gene(
        "Auth module. JWT sessions expire every 15 minutes for docs access.",
        domains=["auth"],
        entities=["jwt"],
        source="/code/auth.py",
    )
    gene_b_id = gb.upsert_gene(gene_b, apply_gate=False)
    gb.conn.close()
    if gb._reader:
        gb._reader.close()

    main = open_main_db(main_path)
    init_main_db(main)
    register_shard(main, "shard_a", "reference", shard_a_path, gene_count=1)
    register_shard(main, "shard_b", "participant", shard_b_path, gene_count=1)
    upsert_fingerprint(
        main, gene_id=gene_a_id, shard_name="shard_a",
        source_id="/docs/README.md",
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
        "gene_a_id": gene_a_id,
        "gene_b_id": gene_b_id,
    }
    td.cleanup()


# Query spans both shards so the genuine cross-shard merge path runs
# (>=2 shards participate -> IDF correction + doc-type boost active).
_DOMAINS = ["auth", "docs"]
_ENTITIES = ["helix", "jwt"]


def test_init_attrs_default_empty(two_shard_setup):
    """__init__ initializes both introspection dicts to {} (no AttributeError)."""
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        assert router.last_score_breakdown == {}
        assert router.last_shard_multipliers == {}
    finally:
        router.close()


def test_flag_off_no_breakdown_and_ranking_unchanged(two_shard_setup):
    """(a) Flag OFF: breakdown stays {}, ranking identical to baseline."""
    assert _DEBUG_ENV not in os.environ

    # Baseline ranking (flag off).
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        baseline = router.query_genes(domains=_DOMAINS, entities=_ENTITIES, max_genes=8)
        baseline_ids = [g.gene_id for g in baseline]
        baseline_scores = dict(router.last_query_scores)
        # Introspection dicts must remain empty when the flag is off.
        assert router.last_score_breakdown == {}
        assert router.last_shard_multipliers == {}
    finally:
        router.close()

    # Second flag-off call on a fresh router must reproduce the exact ranking.
    router2 = ShardRouter(two_shard_setup["main_path"])
    try:
        again = router2.query_genes(domains=_DOMAINS, entities=_ENTITIES, max_genes=8)
        again_ids = [g.gene_id for g in again]
        assert again_ids == baseline_ids
        assert dict(router2.last_query_scores) == baseline_scores
        assert router2.last_score_breakdown == {}
    finally:
        router2.close()


def test_flag_on_breakdown_shape_and_corrected_identity(two_shard_setup):
    """(b) Flag ON: breakdown keys present + corrected == raw*m_shard*boost.

    Also asserts that turning the flag ON does NOT change the returned
    ranking or last_query_scores vs the flag-OFF baseline.
    """
    # Baseline (flag off).
    r0 = ShardRouter(two_shard_setup["main_path"])
    try:
        base = r0.query_genes(domains=_DOMAINS, entities=_ENTITIES, max_genes=8)
        base_ids = [g.gene_id for g in base]
        base_scores = dict(r0.last_query_scores)
    finally:
        r0.close()

    # Flag on.
    os.environ[_DEBUG_ENV] = "1"
    router = ShardRouter(two_shard_setup["main_path"])
    try:
        results = router.query_genes(domains=_DOMAINS, entities=_ENTITIES, max_genes=8)
        ids = [g.gene_id for g in results]

        # Ranking + scores must be unchanged by the flag (behaviour-preserving).
        assert ids == base_ids
        assert dict(router.last_query_scores) == base_scores

        bd = router.last_score_breakdown
        mults = router.last_shard_multipliers

        # Multipliers populated for both participating shards.
        assert set(mults.keys()) == {"shard_a", "shard_b"}
        assert all(isinstance(v, float) for v in mults.values())

        # Breakdown covers EVERY merged candidate (>=2 docs here).
        assert len(bd) >= 2
        expected_keys = {"shard", "raw", "m_shard", "doc_type_boost", "corrected", "rrf"}
        for gid, row in bd.items():
            assert set(row.keys()) == expected_keys, (gid, set(row.keys()))
            assert isinstance(row["shard"], str)
            assert isinstance(row["raw"], float)
            assert isinstance(row["m_shard"], float)
            assert isinstance(row["doc_type_boost"], float)
            assert isinstance(row["corrected"], float)
            # rrf may be a float or None (Fuser may not score every gid).
            assert row["rrf"] is None or isinstance(row["rrf"], float)

            # Core identity: corrected == raw * m_shard * doc_type_boost.
            expected = row["raw"] * row["m_shard"] * row["doc_type_boost"]
            assert row["corrected"] == pytest.approx(expected, rel=1e-9, abs=1e-12), (
                gid, row,
            )

        # The README.md doc (shard_a) must carry the 1.15 doc-type boost.
        gene_a_id = two_shard_setup["gene_a_id"]
        if gene_a_id in bd:
            assert bd[gene_a_id]["doc_type_boost"] == pytest.approx(1.15)
            # And the recorded m_shard must equal the per-shard multiplier.
            assert bd[gene_a_id]["m_shard"] == pytest.approx(mults["shard_a"])
    finally:
        router.close()
        os.environ.pop(_DEBUG_ENV, None)
