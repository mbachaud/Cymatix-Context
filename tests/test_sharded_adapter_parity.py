"""Parity tests for ``ShardedGenomeAdapter`` against the ``KnowledgeStore``
surface that the rest of the codebase reads.

The adapter is hand-maintained and historically lagged ``KnowledgeStore``
additions, which surfaced as HTTP 500s during the bench when the
``HELIX_USE_SHARDS=1`` read path was first driven end-to-end (issue #98).

Two checks here:

1. ``test_adapter_covers_known_caller_surface`` — a hard-coded list of every
   attribute/method that ``context_manager`` and the FastAPI routes actually
   read off ``self.genome`` / ``helix.genome`` today. This is the contract
   the adapter MUST satisfy or callers crash at runtime.

2. ``test_adapter_covers_full_knowledgestore_surface`` — a softer drift
   catcher. Diffs the adapter's attributes against ``KnowledgeStore``'s
   public + sentinel-private surface, with a whitelist for genuinely
   adapter-only-irrelevant items (FTS reindex helpers, etc.).

If you add a new attribute/method on ``KnowledgeStore`` that callers read
via ``self.genome``, either (a) mirror it on the adapter, or (b) add it
to the whitelist with a short note explaining why.
"""

from __future__ import annotations

import tempfile

import pytest

from helix_context.knowledge_store import KnowledgeStore
from helix_context.shard_schema import init_main_db, open_main_db
from helix_context.sharding import ShardedGenomeAdapter


# ── Hard contract: every name read from genome in the current codebase ───
#
# Derived from:
#   grep -oE "self\.genome\.[a-zA-Z_]+"  helix_context/context_manager.py
#   grep -oE "helix\.genome\.[a-zA-Z_]+" helix_context/server/routes_*.py
#
# When a caller adds a new read, add it here. Failure means the adapter
# will AttributeError at runtime for that caller's path.

REQUIRED_CALLER_SURFACE = frozenset({
    # Routing / lifecycle
    "path",
    "close",
    "refresh",
    "checkpoint",

    # Storage / SQL handles
    "conn",
    "read_conn",

    # Retrieval API
    "query_docs",
    "query_docs_ann",
    "query_cold_tier",
    "get_doc",
    "get_gene",  # back-compat alias for get_doc; helpers.py reads via this name
    "get_citation_rows",  # /context citation lookup polymorphism — issue #104

    # Retrieval introspection
    "last_query_scores",

    # Internal flags read by context_manager._retrieve
    "_dense_embedding_enabled",
    "_entity_graph_retrieval_enabled",
    "_last_query_scores_lock",

    # SEMA cache (admin + invalidation paths)
    "_sema_cache",
    "_build_sema_cache",
    "invalidate_sema_cache",
    "_invalidate_dense_matrix",

    # Document write surface (no-ops on adapter, but must exist)
    "upsert_doc",
    "touch_genes",
    "link_coactivated",
    "store_harmonic_weights",
    "store_relations_batch",

    # Lifecycle + maintenance
    "log_health",
    "compress_to_euchromatin",
    "compress_to_heterochromatin",
    "compact",
    "compact_genome",
    "vacuum",
    "set_replication_manager",

    # Stats / health
    "stats",
    "health_summary",
    "health_history",
    "get_calibration_provenance",
})


# ── Soft drift catcher: full KnowledgeStore surface vs adapter ───────────
#
# Items legitimately not on the adapter — write-path internals, FTS5
# rebuild helpers, sema codec wiring, etc. Anything the adapter doesn't
# need to mirror because (a) the sharded read path doesn't exercise it,
# or (b) it's only ever called on the per-shard ``KnowledgeStore`` instance,
# not the adapter.
#
# When a name shows up here unexpectedly, decide: mirror it, or whitelist
# it with a comment.

ADAPTER_ONLY_DIFFERENCES_WHITELIST = frozenset({
    # Chromatin tier sentinels — adapter has no chromatin lifecycle,
    # per-shard Genomes own theirs.
    "TIER_EUCHROMATIN", "TIER_HETEROCHROMATIN", "TIER_OPEN",

    # KnowledgeStore-internal constructor parameters / config snapshots —
    # the adapter holds these on its per-shard Genomes instead.
    "synonym_map", "sema_codec", "splade_enabled", "entity_graph",
    "sr_enabled", "sr_gamma", "sr_k_steps", "sr_weight", "sr_cap",
    "seeded_edges_enabled", "read_only", "_threshold_dim_warned",
    "_dense_pool_size", "_dense_recall_enabled", "ann_similarity_threshold",
    "_reader", "_fusion_mode", "_calibration_provenance",

    # Internal builders / loaders that only make sense on a single .db.
    "make_gene_id", "_load_genes_by_ids", "_score_query",
    "_apply_promoter_tier", "_apply_dense_recall", "_apply_entity_graph",
    "_apply_co_activation_lift", "_apply_seeded_edges_lift",
    "_apply_tcm_bonus", "_apply_freshness_gate", "_pack_codons",
    "_open_reader", "_close_reader", "_path_to_kv",
    "_aggregate_parent_fingerprints", "_apply_authority_boosts",
    "_auto_link_by_entity", "_bm25_candidate_set", "_compact_row_to_gene",
    "_expand_by_entity_graph", "_expand_coactivated", "_expand_terms",
    "_get_dense_codec", "_get_effective_ann_threshold",
    "_init_db", "_refresh_snapshot", "_row_to_gene",

    # FTS / KV / indexing helpers that operate on a single .db file's
    # internal tables.
    "rebuild_fts", "rebuild_path_kv_index", "rebuild_entity_graph",
    "rebuild_dense_matrix", "_dense_matrix", "_ensure_dense_matrix",
    # Tier-0 PR-1 (2026-05-16): inline dense-vector encode at ingest.
    # Write-path helper on the per-shard Genome only — the adapter's
    # upsert_doc is a no-op, so it never encodes.
    "_encode_dense_v2_blob",

    # Dense recall — per-shard, no cross-shard fan-out in V1.
    "query_docs_dense_recall", "query_genes_dense_recall",

    # Cold-tier internals — adapter exposes query_cold_tier as a no-op
    # but the underlying state is per-shard.
    "_cold_tier_enabled", "_cold_tier_k", "_cold_tier_min_cosine",
    "_build_cold_sema_cache", "invalidate_cold_sema_cache",

    # Schema / DDL — handled at shard creation time, not at adapter time.
    "_ensure_schema", "_apply_indexes", "ddl_version",
    "_ensure_registry_schema",

    # Density / calibration / relations — per-shard write-path helpers
    # not consumed via the adapter today. If a route starts reading any
    # of these off helix.genome, move them out of the whitelist and
    # mirror them.
    "apply_density_gate", "compute_density_score", "corpus_size",
    "emit_wal_health_gauges", "get_relations", "store_relation",
    "mark_verified", "upsert_calibration", "reassemble",
})


# ── Test setup ────────────────────────────────────────────────────────────


@pytest.fixture
def empty_main_db():
    """Build a minimal main.genome.db so the adapter can open without shards."""
    td = tempfile.TemporaryDirectory()
    import os
    main_path = os.path.join(td.name, "main.genome.db")
    conn = open_main_db(main_path)
    init_main_db(conn)
    conn.close()
    yield main_path
    td.cleanup()


@pytest.fixture
def adapter(empty_main_db):
    a = ShardedGenomeAdapter(main_path=empty_main_db)
    yield a
    a.close()


# ── Tests ─────────────────────────────────────────────────────────────────


def test_adapter_covers_known_caller_surface(adapter):
    """Every name the codebase reads off ``self.genome`` exists on the adapter.

    This is the regression net for issue #98 — any of these missing
    causes ``AttributeError`` at runtime when a sharded knowledge store is active.
    """
    missing = sorted(name for name in REQUIRED_CALLER_SURFACE if not hasattr(adapter, name))
    assert missing == [], (
        f"ShardedGenomeAdapter is missing {len(missing)} attribute(s) that "
        f"context_manager / routes_*.py read off self.genome: {missing}. "
        f"Either add a (possibly no-op) shim on the adapter, or remove "
        f"the read from the caller."
    )


def test_adapter_path_property_returns_main_path(adapter, empty_main_db):
    """``/admin/swap-db`` reads ``helix.genome.path`` to log the previous DB."""
    assert adapter.path == empty_main_db


def test_adapter_dense_and_entity_graph_flags_default_off(adapter):
    """``context_manager._retrieve`` gates ANN-dense + entity-graph paths
    on these flags. Adapter defaults them off so the legacy non-ANN
    branch runs (which is what the router actually supports in V1)."""
    assert adapter._dense_embedding_enabled is False
    assert adapter._entity_graph_retrieval_enabled is False


def test_adapter_last_query_scores_lock_is_a_lock(adapter):
    """``context_manager`` enters this lock to read scores across sub-query
    threads. Must be a real lock (or compatible) so ``with`` works."""
    # threading.Lock() objects expose acquire/release; use them via `with`.
    with adapter._last_query_scores_lock:
        adapter.last_query_scores = {"abc": 0.5}
    assert adapter.last_query_scores == {"abc": 0.5}


def test_adapter_query_docs_and_query_genes_are_same_callable(adapter):
    """``query_genes`` is a back-compat alias for the R3-canonical
    ``query_docs``. Both must accept the same call shape and route to
    the same underlying router method.

    The router still exposes its read API as ``query_genes``; the
    adapter must bridge regardless of which name the caller uses.
    """
    # Empty-route case: no shards registered, route() returns []
    docs_result = adapter.query_docs(domains=[], entities=[], max_genes=5)
    genes_result = adapter.query_genes(domains=[], entities=[], max_genes=5)
    assert docs_result == genes_result == []


def test_adapter_upsert_doc_and_upsert_gene_are_same_callable(adapter):
    """``upsert_gene`` is a back-compat alias for ``upsert_doc``."""
    from helix_context.schemas import Gene, PromoterTags
    g = Gene(
        gene_id="abc123def456",
        content="x", complement="x", codons=[],
        promoter=PromoterTags(domains=[], entities=[], sequence_index=0),
        source_id="/tmp/x",
    )
    # Both are no-ops returning the gene_id; same callable underneath.
    assert adapter.upsert_doc(g) == "abc123def456"
    assert adapter.upsert_gene(g) == "abc123def456"


def test_adapter_get_doc_and_get_gene_are_same_callable(adapter):
    """``get_gene`` is a back-compat alias for ``get_doc``."""
    # No shards registered, so fingerprint_index is empty -> None.
    assert adapter.get_doc("nonexistent") is None
    assert adapter.get_gene("nonexistent") is None


def test_adapter_declares_fusion_mode_rrf(adapter):
    """Issue #115: ShardRouter unconditionally builds an RRF Fuser
    (``shard_router.py:236``), so the adapter must declare
    ``_fusion_mode = "rrf"`` for ``context_manager._build_signals`` to
    read RRF via ``getattr(self.genome, "_fusion_mode", "additive")``.
    Without this, the abstain absolute-score floor (2.5, BM25-calibrated)
    trips on every sharded query because RRF scores compress to ~0.3.
    """
    assert adapter._fusion_mode == "rrf", (
        "ShardedGenomeAdapter must declare _fusion_mode='rrf' so the "
        "TIGHT/FOCUSED and ABSTAIN floor bypass engages for sharded "
        "reads (issue #115)."
    )


def test_adapter_covers_full_knowledgestore_surface(adapter):
    """Soft drift catcher: warn when KnowledgeStore gains a name the adapter
    doesn't expose, unless explicitly whitelisted.

    This test passing today does not guarantee correctness — a method
    on KnowledgeStore might be present on the adapter but with the wrong
    signature. It catches missing-name drift, which is the most common
    failure mode (and the one #98 was about).
    """
    ks_surface = {
        name for name in dir(KnowledgeStore)
        if not name.startswith("__")
    }
    adapter_surface = {
        name for name in dir(adapter)
        if not name.startswith("__")
    }
    missing = sorted(
        (ks_surface - adapter_surface) - ADAPTER_ONLY_DIFFERENCES_WHITELIST
    )
    assert missing == [], (
        f"ShardedGenomeAdapter is drifting behind KnowledgeStore. "
        f"Names present on KnowledgeStore but missing on the adapter "
        f"(and not whitelisted): {missing}\n"
        f"Either mirror the name on the adapter, or add it to "
        f"ADAPTER_ONLY_DIFFERENCES_WHITELIST with a one-line reason."
    )
