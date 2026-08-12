"""/admin/swap-db must rebuild the store with every boot-path knob.

The 2026-08-09 receipt invalidations came from exactly this drift class:
the boot path (``ContextManager.__init__`` -> ``open_read_source``) threads
a retrieval-affecting constructor kwarg that the hot-swap rebuild in
``routes_admin.admin_swap_db`` does not, so the knob silently resets to its
``KnowledgeStore.__init__`` default after a fixture swap
(filename_anchor/bm25 were the observed casualties; a pki instance was
fixed the same way on claude/encoder-removal-impact-8645c9).

One parametrized case per knob that was missing from the swap path at the
time of the 2026-08-12 audit, plus:

- the ``CYMATIX_FILENAME_ANCHOR_ENABLED`` env override (boot ORs it into
  ``filename_anchor_enabled``; a swap must honor it too),
- the post-construction ``symbol_expansion_cap`` application,
- a structural test pinning the shared kwargs-builder to the full
  config-driven ``KnowledgeStore.__init__`` surface, so the drift class
  dies structurally instead of knob-by-knob (this also covers
  router-only kwargs like ``doc_type_boost_mode`` that the solo store
  accepts but does not persist as an attribute).
"""

from __future__ import annotations

import pytest

from cymatix_context.config import (
    GenomeConfig,
    IngestionConfig,
    RetrievalConfig,
)
from cymatix_context.knowledge_store import KnowledgeStore

from tests.conftest import (
    MockCompressorBackend,
    make_client,
    make_cymatix_config,
)


def _seed_dbs(tmp_path) -> tuple[str, str]:
    """Two empty-but-existing store files to swap between."""
    db_a = str(tmp_path / "a.db")
    db_b = str(tmp_path / "b.db")
    for p in (db_a, db_b):
        KnowledgeStore(path=p).close()
    return db_a, db_b


def _make_app(db_a: str, **section_overrides):
    config = make_cymatix_config(
        genome=GenomeConfig(path=db_a, cold_start_threshold=5),
        **section_overrides,
    )
    client = make_client(config=config, backend=MockCompressorBackend())
    return client.app, client


# Every (config section, config field, non-default value, store attribute)
# that the boot path threaded but the swap path dropped at audit time.
# The value must differ from the KnowledgeStore.__init__ default — a
# dropped kwarg reverts to that default, which is what the test catches.
KNOBS = [
    # ── ingestion-side write knob (matters for read_only=False swaps) ──
    ("ingestion", "dense_embed_on_ingest", True, "_dense_embed_on_ingest"),
    # ── Tier 0.5 filename anchor + BM25 (the 2026-08-09 receipt class) ──
    ("retrieval", "filename_anchor_enabled", True, "_filename_anchor_enabled"),
    ("retrieval", "filename_anchor_weight", 7.25, "_filename_anchor_weight"),
    ("retrieval", "bm25_shortlist_enabled", True, "_bm25_shortlist_enabled"),
    ("retrieval", "bm25_shortlist_size", 123, "_bm25_shortlist_size"),
    ("retrieval", "bm25_prefilter_enabled", True, "_bm25_prefilter_enabled"),
    ("retrieval", "bm25_prefilter_size", 321, "_bm25_prefilter_size"),
    # ── FTS content-tier fetch depth (#205) ──
    ("retrieval", "fts5_candidate_depth", 99, "_fts5_candidate_depth"),
    # ── entity-graph read gate (Tier 5b) ──
    (
        "retrieval",
        "entity_graph_retrieval_enabled",
        True,
        "_entity_graph_retrieval_enabled",
    ),
    # ── post-fusion rerank combinator (#255) + cross-encoder seam (#341) ──
    ("retrieval", "rerank_combinator", "eps_band", "_rerank_combinator"),
    ("retrieval", "rerank_band_delta", 0.42, "_rerank_band_delta"),
    ("retrieval", "rerank_tier_weight", 2.5, "_rerank_tier_weight"),
    ("retrieval", "rerank_enabled", True, "_rerank_enabled"),
    ("retrieval", "rerank_depth", 77, "_rerank_depth"),
    ("retrieval", "rerank_model", "test/dummy-rerank-model", "_rerank_model"),
    # ── per-tier weights (#202: bind in BOTH fusion modes) ──
    ("retrieval", "fts5_weight", 9.0, "_fts5_weight"),
    ("retrieval", "splade_weight", 9.5, "_splade_weight"),
    ("retrieval", "tag_exact_weight", 8.0, "_tag_exact_weight"),
    ("retrieval", "tag_prefix_weight", 4.5, "_tag_prefix_weight"),
    ("retrieval", "sema_boost_weight", 5.5, "_sema_boost_weight"),
    ("retrieval", "sema_cold_weight", 6.5, "_sema_cold_weight"),
    ("retrieval", "lex_anchor_weight", 2.75, "_lex_anchor_weight"),
    ("retrieval", "harmonic_weight", 3.25, "_harmonic_weight"),
    ("retrieval", "entity_graph_weight", 1.25, "_entity_graph_weight"),
    ("retrieval", "pki_weight", 0.0, "_pki_weight"),
    # ── tag/SPLADE IDF discipline (#327 + 2026-08-03 ERB fix) ──
    ("retrieval", "tag_idf_enabled", True, "_tag_idf_enabled"),
    ("retrieval", "tag_df_cap", 0.33, "_tag_df_cap"),
    ("retrieval", "splade_df_cap_fraction", 0.25, "_splade_df_cap_fraction"),
    # ── semantic-wiring arm (2026-06-02) ──
    (
        "retrieval",
        "semantic_dense_additive_weight",
        32.0,
        "_semantic_dense_additive_weight",
    ),
    ("retrieval", "semantic_broaden_routing", False, "_semantic_broaden_routing"),
    # ── source-authority selectivity scaling (#327) ──
    (
        "retrieval",
        "authority_path_selectivity",
        True,
        "_authority_path_selectivity",
    ),
]


@pytest.mark.parametrize(
    ("section", "field", "value", "attr"),
    KNOBS,
    ids=[f"{k[0]}.{k[1]}" for k in KNOBS],
)
def test_swap_db_preserves_knob(tmp_path, section, field, value, attr):
    db_a, db_b = _seed_dbs(tmp_path)

    section_cls = {"retrieval": RetrievalConfig, "ingestion": IngestionConfig}[
        section
    ]
    app, client = _make_app(db_a, **{section: section_cls(**{field: value})})

    # Sanity: the boot path threads it (also pins the attr-name mapping).
    assert getattr(app.state.cymatix.genome, attr) == value, (
        f"boot path did not thread {section}.{field} -> {attr}; "
        "the test's knob mapping is stale"
    )

    resp = client.post("/admin/swap-db", json={"path": db_b})
    assert resp.status_code == 200, resp.json()

    assert getattr(app.state.cymatix.genome, attr) == value, (
        f"/admin/swap-db rebuilt the store with {section}.{field} reset to "
        "its constructor default — boot/swap knob drift "
        "(the 2026-08-09 receipt-invalidation class)"
    )


def test_swap_db_honors_filename_anchor_env_override(tmp_path, monkeypatch):
    """Boot ORs CYMATIX_FILENAME_ANCHOR_ENABLED into the config flag; the
    swap rebuild must apply the same override, not just the raw config."""
    monkeypatch.setenv("CYMATIX_FILENAME_ANCHOR_ENABLED", "1")
    db_a, db_b = _seed_dbs(tmp_path)

    # Config flag stays False — only the env var turns the anchor on.
    app, client = _make_app(db_a)
    assert app.state.cymatix.genome._filename_anchor_enabled is True

    resp = client.post("/admin/swap-db", json={"path": db_b})
    assert resp.status_code == 200, resp.json()

    assert app.state.cymatix.genome._filename_anchor_enabled is True, (
        "swap-db dropped the CYMATIX_FILENAME_ANCHOR_ENABLED env override"
    )


def test_swap_db_applies_symbol_expansion_cap(tmp_path):
    """Boot applies [retrieval] symbol_expansion_cap post-construction
    (WS3, _apply_symbol_expansion_cap); the swap rebuild must too."""
    db_a, db_b = _seed_dbs(tmp_path)

    app, client = _make_app(
        db_a, retrieval=RetrievalConfig(symbol_expansion_cap=17)
    )
    assert getattr(app.state.cymatix.genome, "_symbol_expansion_cap", None) == 17

    resp = client.post("/admin/swap-db", json={"path": db_b})
    assert resp.status_code == 200, resp.json()

    assert getattr(app.state.cymatix.genome, "_symbol_expansion_cap", None) == 17, (
        "swap-db rebuilt the store without re-applying symbol_expansion_cap"
    )


def test_shared_builder_covers_every_config_driven_ctor_knob():
    """Structural guard: the shared kwargs-builder must cover the full
    config-driven KnowledgeStore.__init__ surface.

    A new constructor knob threaded at boot but not added to the builder
    (or explicitly allowlisted below as non-config plumbing) fails here,
    so the boot/swap drift class cannot silently reopen.
    """
    import inspect

    from cymatix_context.sharding import build_genome_kwargs

    sig = inspect.signature(KnowledgeStore.__init__)
    # Plumbing that is deliberately NOT config-driven: identity/path args,
    # per-call DI seams, and sharded-internals wiring.
    non_config = {
        "self",
        "path",
        "rerank_scorer",  # test-only DI seam (#341) — never config-threaded
        "main_conn",
        "shard_name",
        "read_only",
        "mem_plan",
    }
    expected = set(sig.parameters) - non_config

    built = set(build_genome_kwargs(make_cymatix_config()))

    missing = expected - built
    assert not missing, (
        f"build_genome_kwargs is missing config-driven ctor knobs: "
        f"{sorted(missing)} — thread them (or allowlist as non-config)"
    )
    stray = built - set(sig.parameters)
    assert not stray, (
        f"build_genome_kwargs emits kwargs KnowledgeStore.__init__ does not "
        f"accept: {sorted(stray)}"
    )
