"""Parity tests for ``ShardedGenomeAdapter`` against the ``KnowledgeStore``
surface that the rest of the codebase reads.

The adapter is hand-maintained and historically lagged ``KnowledgeStore``
additions, which surfaced as HTTP 500s during the bench when the
``HELIX_USE_SHARDS=1`` read path was first driven end-to-end (issue #98).

One merged surface check here (2026-07-05 test-suite consolidation, Task 9
folded the former two overlapping tests into a single
``test_adapter_covers_full_knowledgestore_surface``):

``test_adapter_covers_full_knowledgestore_surface`` asserts the union of:

1. A hard-coded list (``REQUIRED_CALLER_SURFACE``) of every
   attribute/method that ``context_manager`` and the FastAPI routes
   actually read off ``self.genome`` / ``helix.genome`` today. This is
   the contract the adapter MUST satisfy or callers crash at runtime.
   Several of these (``conn``, ``path``, ``last_query_scores``, ...) are
   instance-only attributes on ``KnowledgeStore`` that never show up in
   ``dir(KnowledgeStore)`` (the class) — the union keeps this hard
   contract even though check 2 alone would miss them.

2. A softer drift catcher: every other name on ``KnowledgeStore`` (public
   + sentinel-private, via ``dir(KnowledgeStore)``), with a whitelist for
   genuinely adapter-only-irrelevant items (FTS reindex helpers, etc.).

If you add a new attribute/method on ``KnowledgeStore`` that callers read
via ``self.genome``, either (a) mirror it on the adapter, or (b) add it
to the whitelist with a short note explaining why.
"""

from __future__ import annotations

import tempfile

import pytest

from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.shard_schema import init_main_db, open_main_db
from cymatix_context.sharding import ShardedGenomeAdapter


# ── Hard contract: every name read from genome in the current codebase ───
#
# Derived from:
#   grep -oE "self\.genome\.[a-zA-Z_]+"  cymatix_context/context_manager.py
#   grep -oE "helix\.genome\.[a-zA-Z_]+" cymatix_context/server/routes_*.py
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
    # Issue #260: per-shard RRF gate resolver. Called on `self` inside each
    # per-shard KnowledgeStore.query_genes; the adapter's read path fuses via
    # the ShardRouter's own (ungated) Fuser, so it never resolves this.
    "_rrf_gate_params",
    "_init_db", "_refresh_snapshot", "_row_to_gene",

    # FTS / KV / indexing helpers that operate on a single .db file's
    # internal tables.
    "rebuild_fts", "rebuild_path_kv_index", "rebuild_entity_graph",
    "rebuild_dense_matrix", "_dense_matrix", "_ensure_dense_matrix",
    # Tier-0 PR-1 (2026-05-16): inline dense-vector encode at ingest.
    # Write-path helper on the per-shard Genome only — the adapter's
    # upsert_doc is a no-op, so it never encodes.
    "_encode_dense_v2_blob",

    # #182 cross-shard global-IDF lexical re-score. Per-shard helpers the
    # ShardRouter calls DIRECTLY on each Genome (not through the adapter):
    # the router aggregates global N/df across shards, then asks each shard
    # to recompute its candidates' BM25 lexical sub-score with the injected
    # global IDF. No adapter fan-out — they operate on one .db's FTS index.
    "rescore_lexical_global_idf", "_ensure_fts_vocab",

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
    from cymatix_context.schemas import Gene, PromoterTags
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


def test_adapter_declares_fusion_mode_additive(adapter):
    """Tier-0 PR-3 (2026-05-16), Decision 2 Option (b): the adapter
    declares ``_fusion_mode = "additive"`` — the honest label.

    The ShardRouter builds an RRF ``Fuser`` but uses its ``all_scores()``
    only as a secondary sort tiebreaker; the primary sort key and the
    published ``last_query_scores`` map are the IDF-corrected
    *additive*-scale ``corrected`` scores. The router never publishes
    RRF-scale numbers. The previous ``"rrf"`` label (issue #115) made
    ``tier_logic`` skip its absolute floors on every sharded query —
    treating a symptom of the mislabel. With ``"additive"`` the gates
    interpret the BM25-ish sharded scores on the scale they were
    calibrated for, so ``skip_absolute_floors`` is False and the
    absolute floors run.
    """
    assert adapter._fusion_mode == "additive", (
        "ShardedGenomeAdapter must declare _fusion_mode='additive' so the "
        "downstream gates interpret the router's IDF-corrected "
        "additive-scale scores on the correct scale (Tier-0 PR-3, "
        "Decision 2 Option (b))."
    )


def test_sharded_fusion_mode_runs_absolute_floors_in_tier_logic(adapter):
    """Tier-0 PR-3, Decision 2 Option (b) — behavioural check.

    The adapter's ``_fusion_mode`` flows into ``pipeline.tier_logic`` via
    ``context_manager``'s ``getattr(self.genome, "_fusion_mode",
    "additive")``. With the honest ``"additive"`` label,
    ``tier_logic.apply_budget_tiers`` sets ``skip_absolute_floors`` to
    False (it is ``fusion_mode == "rrf"``), so the absolute
    TIGHT/FOCUSED/ABSTAIN floors RUN on sharded queries — they were
    skipped under the pre-PR-3 ``"rrf"`` mislabel.

    Proof: a score distribution (top=0.40, gradient to 0.05) whose
    legacy ``top/mean`` ratio is ~1.78 (< the 1.8 abstain ratio floor)
    and whose top is far below the 2.5 absolute abstain floor. Fed the
    adapter's actual ``_fusion_mode``:
      - "additive" -> absolute floor active, ratio gate trips -> ABSTAIN
      - "rrf"      -> absolute floor SKIPPED, baseline-normalized ratio
                      is 2.0 (>= 1.5) -> NO abstain
    The outcomes diverge, so this pins that the floors actually run for
    the sharded path under the corrected label.
    """
    from cymatix_context.config import AbstainClassFloors
    from cymatix_context.pipeline.tier_logic import apply_budget_tiers
    from tests.conftest import make_gene

    n = 12
    top, low = 0.40, 0.05
    step = (top - low) / (n - 1)
    candidates = [
        make_gene(f"shard_{i}", gene_id=f"shard_gene_{i:010d}")
        for i in range(n)
    ]
    scores = {candidates[i].gene_id: top - i * step for i in range(n)}

    # The adapter declares "additive"; feed exactly that to tier_logic.
    result_sharded = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode=adapter._fusion_mode,
    )
    assert result_sharded.abstain is True, (
        "with the honest 'additive' label the absolute abstain floor runs "
        "and this weak distribution abstains"
    )

    # Counterfactual: the pre-PR-3 "rrf" mislabel would have skipped the
    # floor and NOT abstained on the identical distribution.
    result_mislabel = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(),
        abstain_enabled=True, fusion_mode="rrf",
    )
    assert result_mislabel.abstain is False, (
        "sanity: the same distribution under the old 'rrf' label skips "
        "the absolute floor and does not abstain — confirms the relabel "
        "changes gate behaviour, which is the point of Option (b)"
    )


def test_adapter_resolve_symbol_empty_route_returns_empty(adapter):
    """``resolve_symbol`` is a read — it must fan out across shards (like
    ``term_doc_frequencies``), and with no shards registered it returns []."""
    assert adapter.resolve_symbol("AnySymbol") == []


def test_adapter_symbol_write_surface_is_noop(adapter):
    """WS2 symbol-graph writes are per-shard ingest-path work; on the V1
    read-only adapter they are silent no-ops (same contract as
    ``store_relations_batch``)."""
    adapter.store_symbol_defs([("sym", "gene1", "function")])  # must not raise
    assert adapter.resolve_symbol("sym") == []  # nothing persisted
    assert adapter._sweep_symbol_orphans() == 0


def test_adapter_delete_gene_is_noop_returning_false(adapter):
    """``delete_gene`` is a write — V1 no-op. Returns False (the
    KnowledgeStore contract's 'id unknown / nothing deleted' value) so
    admin callers don't believe a hard-delete happened."""
    assert adapter.delete_gene("abc123def456") is False


def test_adapter_covers_full_knowledgestore_surface(adapter):
    """Full surface check: union of the hard caller contract and the
    softer KnowledgeStore drift catcher (merged 2026-07-05, Task 9 — see
    module docstring).

    1. Hard contract: every name in ``REQUIRED_CALLER_SURFACE`` (what
       ``context_manager`` / the FastAPI routes actually read off
       ``self.genome`` today) must exist on the adapter — miss it and
       callers hit ``AttributeError`` at runtime (issue #98).
    2. Soft drift catcher: every other name on ``KnowledgeStore`` (public
       + sentinel-private, via ``dir(KnowledgeStore)``) must also exist
       on the adapter unless whitelisted. This test passing today does
       not guarantee correctness — a method on KnowledgeStore might be
       present on the adapter but with the wrong signature — but it
       catches missing-name drift, the most common failure mode.

    ``REQUIRED_CALLER_SURFACE`` is unioned in explicitly because it
    includes instance-only attributes (``conn``, ``path``,
    ``last_query_scores``, ...) that never show up in
    ``dir(KnowledgeStore)`` (the class) — check 2 alone would miss them.
    """
    ks_surface = {
        name for name in dir(KnowledgeStore)
        if not name.startswith("__")
    }
    full_surface = ks_surface | REQUIRED_CALLER_SURFACE
    adapter_surface = {
        name for name in dir(adapter)
        if not name.startswith("__")
    }
    # Scope the whitelist so it can never exempt a hard-contract name:
    # REQUIRED_CALLER_SURFACE entries stay unconditionally required even if
    # someone later adds one to ADAPTER_ONLY_DIFFERENCES_WHITELIST.
    exemptable = ADAPTER_ONLY_DIFFERENCES_WHITELIST - REQUIRED_CALLER_SURFACE
    missing = sorted(
        (full_surface - adapter_surface) - exemptable
    )
    assert missing == [], (
        f"ShardedGenomeAdapter is missing {len(missing)} attribute(s) from "
        f"the required caller surface and/or drifting behind KnowledgeStore. "
        f"Names required but missing on the adapter (and not whitelisted): "
        f"{missing}\n"
        f"Either mirror the name on the adapter (with a possibly no-op "
        f"shim), or add it to ADAPTER_ONLY_DIFFERENCES_WHITELIST with a "
        f"one-line reason."
    )
