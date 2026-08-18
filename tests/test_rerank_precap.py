"""Issue #341: pre-cap cross-encoder rerank seams in ``knowledge_store``.

Two seams, one contract:

* ``query_docs_ann`` — the cross-encoder judges the *union* pool (sim head
  PLUS every gate-kept id beyond it, so #214 floor rescues are re-scored
  rather than blindly evicted) and hands back exactly ``len(gate_ids)``
  documents. The gate decides HOW MANY; the cross-encoder decides WHICH.
* ``query_docs`` — same idea one layer down, after the RRF/additive fusion
  branch, so the legacy ``fusion_mode="additive"`` profile is covered too.
  Fires only when the caller supplies ``query_text`` (the ANN path
  deliberately withholds it — that is the double-rerank suppression).

**Count invariant** (the splice-cliff receipt depends on it): for identical
inputs, ``len(rerank_on) == len(rerank_off)``. Always. Membership and order
may change; the count may not.

No model is loaded anywhere in this file: every test injects a fake scorer
through the ``rerank_scorer`` DI seam, and the dense leg is a canned
``[(gene_id, cosine)]`` list, so the union order and the ANN gate are fully
deterministic.
"""
from __future__ import annotations

import inspect
import logging

import pytest

from cymatix_context.genome import Genome
from cymatix_context.knowledge_store import KnowledgeStore, _rerank_doc_text
from cymatix_context.schemas import (
    ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
)

# ── Corpus shape ─────────────────────────────────────────────────────────
_POOL = 30            # documents, all tagged "topic"
_MARKER = "xylophone"  # appears in exactly ONE document's content
_MARKER_IDX = 25      # ...that document's index -> gene_id "g-25"
_MARKER_DENSE_RANK = 20  # where the canned dense leg puts it (below any cut)


def _gid(i: int) -> str:
    # Zero-padded so the RRF (-score, gene_id) tie-break gives a stable,
    # predictable lexical order: g-00 first, g-29 last.
    return f"g-{i:02d}"


def _make_gene(content: str, *, gene_id: str, summary: str = "") -> Gene:
    return Gene(
        gene_id=gene_id,
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=["topic"], entities=[], summary=summary),
        epigenetics=EpigeneticMarkers(),
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
    )


def _build_pool(store) -> None:
    """~30 documents tagged ``"topic"``; exactly one carries the marker.

    Deliberately avoids the literal token "topic" in the bodies so Tier-3
    FTS5 stays silent and the lexical ranking collapses to the tag-exact
    tier alone — i.e. a clean gene_id-ascending order.
    """
    for i in range(_POOL):
        word = _MARKER if i == _MARKER_IDX else f"filler{i:02d}"
        content = (
            f"Document {i:02d} covers the subject matter at hand. "
            f"It discusses {word} in enough detail that the body is "
            f"non-trivial prose rather than a stub."
        )
        # Issue #341: the sanctioned pair-text variation scores
        # ``promoter.summary`` (not ``content``) primarily, so the summary
        # must carry the same content markers as the body for these fixtures
        # to stay meaningful under the new form — the marker doc's summary
        # gets the marker too, same as its content.
        store.upsert_gene(
            _make_gene(content, gene_id=_gid(i), summary=content), apply_gate=False
        )


def _canned_dense_order() -> list[str]:
    """gene_ids in dense-cosine order, marker parked at ``_MARKER_DENSE_RANK``."""
    ids = [_gid(i) for i in range(_POOL) if i != _MARKER_IDX]
    ids.insert(_MARKER_DENSE_RANK, _gid(_MARKER_IDX))
    return ids


def _scorer_favoring(marker: str):
    """Cross-encoder stand-in: marker-bearing text wins, everything else loses."""
    def score(query, texts):
        return [1.0 if marker in t else 0.01 for t in texts]
    return score


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def store_with_pool():
    """Dense-enabled store whose dense leg is a canned, deterministic list."""
    store = Genome(
        path=":memory:",
        dense_embedding_enabled=True,
        ann_similarity_threshold=0.58,
        ann_threshold_min_genes=1,
        ann_threshold_max_genes=12,
        dense_pool_floor_genes=8,
        dense_pool_size=500,
        fusion_mode="rrf",
    )
    _build_pool(store)
    # 0.90 .. 0.755 — every candidate clears the 0.58 threshold, so the gate
    # is a pure max_genes cap and the marker (rank 20) is cleanly outside it.
    canned = [
        (gid, 0.90 - 0.005 * i) for i, gid in enumerate(_canned_dense_order())
    ]
    store.query_docs_dense_recall = lambda *a, **kw: list(canned)
    yield store
    store.close()


@pytest.fixture
def lex_store_with_pool():
    """Dense-disabled store — exercises the ``query_docs`` fusion seam."""
    store = Genome(
        path=":memory:",
        dense_embedding_enabled=False,
        fusion_mode="rrf",
    )
    _build_pool(store)
    yield store
    store.close()


# ═══ 1. ANN seam — count preserved, membership flipped ═══════════════════


def test_ann_rerank_preserves_gate_count_and_changes_membership(store_with_pool):
    g = store_with_pool
    off = g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    assert _MARKER not in off[0].content          # the gate never saw it

    g._rerank_enabled, g._rerank_scorer = True, _scorer_favoring(_MARKER)
    on = g.query_docs_ann("query text", domains=["topic"], max_genes=12)

    assert len(on) == len(off)                    # splice-cliff invariant
    assert _MARKER in on[0].content               # cross-encoder decided order
    assert [d.gene_id for d in on] != [d.gene_id for d in off]


def test_ann_rerank_off_is_byte_identical(store_with_pool):
    g = store_with_pool
    a = [d.gene_id for d in g.query_docs_ann("query text", domains=["topic"], max_genes=12)]
    g._rerank_scorer = _scorer_favoring(_MARKER)   # scorer set but knob off
    b = [d.gene_id for d in g.query_docs_ann("query text", domains=["topic"], max_genes=12)]
    assert a == b


def test_rerank_override_beats_global(store_with_pool):
    g = store_with_pool
    g._rerank_enabled, g._rerank_scorer = False, _scorer_favoring(_MARKER)
    on = g.query_docs_ann(
        "query text", domains=["topic"], max_genes=12, rerank_override=True
    )
    assert _MARKER in on[0].content

    g._rerank_enabled = True
    off = g.query_docs_ann(
        "query text", domains=["topic"], max_genes=12, rerank_override=False
    )
    assert _MARKER not in off[0].content


# ═══ 2. Failure containment — the store never raises out of the scorer ═══


def test_scorer_failure_falls_back_to_gate_order(store_with_pool, caplog):
    g = store_with_pool

    def boom(q, t):
        raise RuntimeError("model exploded")

    g._rerank_enabled, g._rerank_scorer = True, boom
    off_ids = [
        d.gene_id for d in g.query_docs_ann(
            "query text", domains=["topic"], max_genes=12, rerank_override=False
        )
    ]
    with caplog.at_level(logging.WARNING, logger="cymatix_context.knowledge_store"):
        on_ids = [
            d.gene_id for d in g.query_docs_ann(
                "query text", domains=["topic"], max_genes=12
            )
        ]
    assert on_ids == off_ids
    assert any("rerank" in r.message.lower() for r in caplog.records)


def test_unusable_scorer_payload_falls_back(store_with_pool, caplog):
    """Wrong-length / wrong-type payloads are a fallback, not a crash."""
    g = store_with_pool
    off_ids = [
        d.gene_id for d in g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    ]
    g._rerank_enabled = True
    for bad in (lambda q, t: [1.0], lambda q, t: None, lambda q, t: "nope"):
        g._rerank_scorer = bad
        with caplog.at_level(logging.WARNING, logger="cymatix_context.knowledge_store"):
            ids = [
                d.gene_id for d in g.query_docs_ann(
                    "query text", domains=["topic"], max_genes=12
                )
            ]
        assert ids == off_ids
    assert any("rerank" in r.message.lower() for r in caplog.records)


# ═══ 3. #214 floor rescues are judged, never blind-evicted ═══════════════


def test_floor_rescues_enter_the_candidate_set(store_with_pool):
    """Threshold above every cosine -> the gate output IS the floor slice.

    ``rerank_depth`` is set below the floor slice on purpose, so the rescued
    ids sit *beyond* the sim head. They must still be scored (candidate set =
    head + gate-kept-beyond-head), and the returned count must equal the
    gate's, which here exceeds ``max_genes``' own contribution.
    """
    g = store_with_pool
    g._ann_threshold = 0.99          # above the canned 0.90 max
    g._rerank_depth = 5

    off = g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    assert len(off) == 8             # 1 min_genes survivor + 7 floor rescues
    assert g.last_rerank_diag == {"gate_kept": 8, "floor_appended": 3}

    g._rerank_enabled, g._rerank_scorer = True, _scorer_favoring(_MARKER)
    on = g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    assert len(on) == len(off)
    assert g.last_rerank_diag == {"gate_kept": 8, "floor_appended": 3}


def test_count_invariant_holds_when_a_gate_kept_id_has_no_body(store_with_pool):
    """A floor rescue can name an id with no ``genes`` row (stale dense
    matrix). Both arms drop it — so the ON arm must NOT back-fill the slot."""
    g = store_with_pool
    ghost = [("g-ghost", 0.95)] + [
        (gid, 0.90 - 0.005 * i) for i, gid in enumerate(_canned_dense_order())
    ]
    g.query_docs_dense_recall = lambda *a, **kw: list(ghost)
    g._ann_threshold = 0.99          # gate starves -> the floor does the work

    off = g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    assert g.last_rerank_diag["gate_kept"] == 8    # the gate kept 8...
    assert len(off) == 7                           # ...but one has no body

    g._rerank_enabled, g._rerank_scorer = True, _scorer_favoring(_MARKER)
    on = g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    assert len(on) == len(off)


# ═══ 4. Diagnostics: last_ranked_ids / last_rerank_diag / timings ════════


def test_last_ranked_ids_published_both_arms(store_with_pool):
    g = store_with_pool
    g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    off_order = list(g.last_ranked_ids)
    assert off_order                                  # published with rerank OFF

    g._rerank_enabled, g._rerank_scorer = True, _scorer_favoring(_MARKER)
    g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    on_order = list(g.last_ranked_ids)

    assert set(off_order) == set(on_order)            # same pool, different order
    assert on_order != off_order
    assert on_order[0] == _gid(_MARKER_IDX)
    assert "xenc_rerank" in g.last_signal_timings


def test_last_ranked_ids_published_on_the_lex_path(lex_store_with_pool):
    g = lex_store_with_pool
    g.query_docs(["topic"], [], max_genes=12)
    off_order = list(g.last_ranked_ids)
    assert off_order

    g._rerank_enabled, g._rerank_scorer = True, _scorer_favoring(_MARKER)
    g.query_docs(["topic"], [], max_genes=12, query_text="query text")
    on_order = list(g.last_ranked_ids)
    assert on_order[0] == _gid(_MARKER_IDX)
    assert on_order != off_order
    assert "xenc_rerank" in g.last_signal_timings


def test_last_ranked_ids_depth_differs_by_path(store_with_pool):
    """Pins the documented asymmetry so it can't drift silently.

    ``query_docs_ann`` publishes the FULL union pool (above and below the
    gate); ``query_docs`` publishes only the post-cut ``max_genes * 2`` list.
    A consumer computing rank-of-gold has to know which it is reading — on the
    lex path a gold below the cut is absent, i.e. unmeasurable, not rank 0.
    """
    g = store_with_pool
    g.query_docs_ann("query text", domains=["topic"], max_genes=8)
    assert len(g.last_ranked_ids) == _POOL          # whole union pool

    g.query_docs(["topic"], [], max_genes=8)
    assert len(g.last_ranked_ids) == 16             # post-cut: max_genes * 2


def test_last_ranked_ids_published_under_additive_fusion():
    store = Genome(path=":memory:", dense_embedding_enabled=False, fusion_mode="additive")
    try:
        _build_pool(store)
        store.query_docs(["topic"], [], max_genes=12)
        assert store.last_ranked_ids                  # additive branch too

        store._rerank_enabled = True
        store._rerank_scorer = _scorer_favoring(_MARKER)
        out = store.query_docs(["topic"], [], max_genes=12, query_text="query text")
        assert _MARKER in out[0].content
        assert store.last_ranked_ids[0] == _gid(_MARKER_IDX)
    finally:
        store.close()


# ═══ 5. query_docs seam (dense off) ══════════════════════════════════════


def test_lex_path_rerank_via_query_text(lex_store_with_pool):
    g = lex_store_with_pool
    g._rerank_enabled, g._rerank_scorer = True, _scorer_favoring(_MARKER)
    plain = g.query_docs(["topic"], [], max_genes=12)      # no query_text -> no rerank
    rr = g.query_docs(["topic"], [], max_genes=12, query_text="query text")
    assert len(plain) == len(rr)
    assert _MARKER not in plain[0].content
    assert _MARKER in rr[0].content


def test_lex_path_rerank_survives_splade_tier_shadowing(monkeypatch):
    """Regression (Task 6 self-review, issue #341): the SPLADE tier used to
    reassign a LOCAL variable also named ``query_text`` (``query_text = "
    ".join(query_terms)`` for its own sparse encode), silently clobbering the
    function parameter before the cross-encoder rerank seam read it a few
    hundred lines later — so with SPLADE enabled (the shipped
    ``[ingestion] splade_enabled`` default), the reranker scored against a
    keyword bag (e.g. ``"topic"``) instead of the caller's actual question.
    Pins that the raw ``query_text`` now survives SPLADE running in the same
    call. ``splade_backend.encode``/``query_splade`` are monkeypatched to
    cheap fakes so this test loads no real model (file contract)."""
    from cymatix_context.backends import splade_backend

    monkeypatch.setattr(splade_backend, "encode", lambda text, **kw: {})
    monkeypatch.setattr(
        splade_backend, "query_splade",
        lambda conn, sparse, limit=20, **kw: [],
    )

    g = Genome(path=":memory:", dense_embedding_enabled=False,
               splade_enabled=True, fusion_mode="rrf")
    try:
        _build_pool(g)
        seen = {}

        def spy_scorer(query, texts):
            seen["query"] = query
            return [1.0 if _MARKER in t else 0.01 for t in texts]

        g._rerank_enabled, g._rerank_scorer = True, spy_scorer
        g.query_docs(["topic"], [], max_genes=12, query_text="query text")
    finally:
        g.close()
    assert seen["query"] == "query text"       # not "topic" (the keyword bag)


def test_lex_path_rerank_widens_pool_but_preserves_count(lex_store_with_pool):
    """The marker sits outside the pre-seam cut; widening lets it in.

    Count still comes from ``limit`` (= max_genes * 2), never from the
    widened rerank depth.
    """
    g = lex_store_with_pool
    plain_ids = [d.gene_id for d in g.query_docs(["topic"], [], max_genes=12)]
    assert _gid(_MARKER_IDX) not in plain_ids             # below the cut when OFF

    g._rerank_enabled, g._rerank_scorer = True, _scorer_favoring(_MARKER)
    rr = g.query_docs(["topic"], [], max_genes=12, query_text="query text")
    assert len(rr) == len(plain_ids)
    assert rr[0].gene_id == _gid(_MARKER_IDX)


def test_lex_count_invariant_on_a_stale_index_store():
    """A ranked id with no ``genes`` row must not become a free ON-arm slot.

    Stale-index shape: the dense leg still names a document whose row is
    gone (matrix cached across a delete), so it ranks but body-fetches to
    nothing. Both arms drop it — so the reranked head has to keep it in
    *its slot* rather than pushing it past the cut, which would let one more
    loadable document into the window on the ON arm only.
    """
    store = Genome(
        path=":memory:",
        dense_embedding_enabled=True,
        fusion_mode="additive",
        # Puts the phantom's cosine above the tag-exact + lex-anchor stack the
        # real documents accumulate, so it ranks into the delivered window.
        dense_additive_weight=40.0,
    )
    try:
        _build_pool(store)
        store.query_docs_dense_recall = lambda *a, **kw: [("g-ghost", 0.95)]

        off = store.query_docs(["topic"], [], max_genes=8)
        off_ranked = list(store.last_ranked_ids)
        assert "g-ghost" in off_ranked[:16]      # precondition: inside the cut
        assert len(off) < 16                     # ...and it costs a slot

        store._rerank_enabled = True
        store._rerank_scorer = _scorer_favoring(_MARKER)
        on = store.query_docs(["topic"], [], max_genes=8, query_text="query text")

        assert len(on) == len(off)               # the invariant
        assert _MARKER in on[0].content          # rerank still did its job
    finally:
        store.close()


def test_lex_scorer_failure_falls_back(lex_store_with_pool, caplog):
    g = lex_store_with_pool
    off_ids = [d.gene_id for d in g.query_docs(["topic"], [], max_genes=12)]

    def boom(q, t):
        raise RuntimeError("model exploded")

    g._rerank_enabled, g._rerank_scorer = True, boom
    with caplog.at_level(logging.WARNING, logger="cymatix_context.knowledge_store"):
        on_ids = [
            d.gene_id for d in g.query_docs(
                ["topic"], [], max_genes=12, query_text="query text"
            )
        ]
    assert on_ids == off_ids                              # widened pool truncated back
    assert any("rerank" in r.message.lower() for r in caplog.records)


# ═══ 6. Double-rerank suppression ════════════════════════════════════════


def test_ann_path_scores_exactly_once(store_with_pool):
    """``query_docs_ann`` must not hand ``query_text`` to its inner
    ``query_docs`` call — otherwise every ANN query reranks twice."""
    g = store_with_pool
    calls = []

    def counting(q, t):
        calls.append(len(t))
        return [1.0 if _MARKER in x else 0.01 for x in t]

    g._rerank_enabled, g._rerank_scorer = True, counting
    g.query_docs_ann("query text", domains=["topic"], max_genes=12)
    assert len(calls) == 1


# ═══ 7. Default-inert construction ═══════════════════════════════════════


def test_ctor_defaults_are_inert():
    sig = inspect.signature(KnowledgeStore.__init__)
    assert sig.parameters["rerank_enabled"].default is False
    assert sig.parameters["rerank_depth"].default == 50
    assert sig.parameters["rerank_model"].default == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert sig.parameters["rerank_scorer"].default is None

    ks = KnowledgeStore(path=":memory:")
    try:
        assert ks._rerank_enabled is False
        assert ks._rerank_depth == 50
        assert ks._rerank_scorer is None
        assert ks.last_ranked_ids == []
        assert ks.last_rerank_diag == {}
    finally:
        ks.close()


def test_ctor_threads_rerank_knobs():
    ks = KnowledgeStore(
        path=":memory:",
        rerank_enabled=True,
        rerank_depth=7,
        rerank_model="some/other-model",
    )
    try:
        assert ks._rerank_enabled is True
        assert ks._rerank_depth == 7
        assert ks._rerank_model == "some/other-model"
    finally:
        ks.close()


def test_rerank_effective_resolution():
    ks = KnowledgeStore(path=":memory:")
    try:
        assert ks._rerank_effective(None) is False
        assert ks._rerank_effective(True) is True
        ks._rerank_enabled = True
        assert ks._rerank_effective(None) is True
        assert ks._rerank_effective(False) is False
    finally:
        ks.close()


def test_rerank_backend_import_is_lazy():
    """Importing the store must never pull torch/transformers into the process.

    The backend import lives inside ``_score_rerank`` — a module-level one
    would make every ``import cymatix_context.knowledge_store`` load torch,
    including for stores that never rerank.
    """
    import sys

    assert (
        "from .backends.rerank_backend import score_pairs"
        in inspect.getsource(KnowledgeStore._score_rerank)
    )
    module_src = inspect.getsource(sys.modules[KnowledgeStore.__module__])
    top_level = [
        ln for ln in module_src.splitlines() if ln.startswith(("import ", "from "))
    ]
    assert not [ln for ln in top_level if "rerank_backend" in ln or "torch" in ln]


# ═══ 8. Manager threading (config -> store, per-class override, query_text) ══
#
# Issue #341 Task 6. Mirrors ``_seeded_manager`` / ``_spy_combinator`` in
# ``test_classifier_gated_combinator.py`` (:375 / :411) for the sibling knob:
# ``[retrieval] rerank_enabled`` / ``rerank_depth`` / ``rerank_model`` thread
# manager -> store construction, and ``resolve_class_flag`` (rerank_combinators.py)
# resolves a per-turn ``rerank_override`` from the stage-0 classifier class,
# threaded through ``_retrieve`` into ``query_docs_ann`` / ``query_docs``. The
# dense-off ``query_docs`` branch additionally must pass ``query_text=<the raw
# query>`` so Task 5's lex-path rerank seam can fire at all.


def _seeded_manager_cls_gated(
    rerank_map: dict,
    rerank_enabled: bool = False,
    classifier_enabled: bool = True,
    dense_embedding_enabled: bool = True,
):
    """A mock-backend, in-memory-genome manager seeded so ``build_context``
    reaches retrieval, with ``[retrieval] rerank_enabled_by_class`` /
    ``rerank_enabled`` set for THIS test. Adapted from ``_seeded_manager`` in
    ``test_classifier_gated_combinator.py:375`` (same corpus + classifier
    setup). ``dense_embedding_enabled`` defaults True here (the shipped
    default is False since 2026-08-15) so
    the ANN path (``query_docs_ann``) is exercised by default; a test that
    wants the plain lex ``query_docs`` seam instead forces it False, same as
    the classifier-gated-combinator manager fixture does unconditionally.
    """
    from cymatix_context.config import (
        BudgetConfig, ClassifierConfig, GenomeConfig, CymatixConfig,
        RetrievalConfig, RibosomeConfig,
    )
    from cymatix_context.context_manager import CymatixContextManager
    from tests.conftest import MockCompressorBackend, make_gene

    cfg = CymatixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=4, splice_aggressiveness=0.5),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=classifier_enabled),
        retrieval=RetrievalConfig(
            fusion_mode="rrf",
            dense_embedding_enabled=dense_embedding_enabled,
            rerank_enabled_by_class=rerank_map,
            rerank_enabled=rerank_enabled,
        ),
    )
    mgr = CymatixContextManager(cfg)
    mgr.ribosome.backend = MockCompressorBackend()
    for i, (content, doms, ents) in enumerate([
        ("what is the auth middleware path in the server module",
         ["auth", "server"], ["middleware", "auth", "path"]),
        ("auth middleware lives in server routes and handlers",
         ["auth", "server"], ["middleware", "routes"]),
        ("hello there general notes about greetings",
         ["greeting"], ["hello"]),
        ("more hello there phrasing examples",
         ["greeting"], ["hello"]),
    ]):
        mgr.genome.upsert_gene(
            make_gene(content, domains=doms, entities=ents,
                      gene_id=f"seed_gene_{i:010d}"),
        )
    return mgr


@pytest.fixture
def seeded_manager_cls_gated():
    return _seeded_manager_cls_gated


def test_manager_config_threads_rerank_knobs_into_store(seeded_manager_cls_gated):
    """Config -> store: [retrieval] rerank_enabled/rerank_depth/rerank_model
    reach the constructed store verbatim (the ``open_read_source`` kwarg fan)."""
    mgr = seeded_manager_cls_gated(rerank_map={}, rerank_enabled=True)
    try:
        assert mgr.genome._rerank_enabled is True
        assert mgr.genome._rerank_depth == mgr.config.retrieval.rerank_depth
        assert mgr.genome._rerank_model == mgr.config.retrieval.rerank_model
    finally:
        mgr.close()


def test_manager_threads_per_class_rerank_override(monkeypatch, seeded_manager_cls_gated):
    """A wh-question classes ``factual``; the map routes factual -> True, and
    the override reaches ``query_docs_ann`` as ``rerank_override`` even
    though the store's own global ``rerank_enabled`` is False."""
    mgr = seeded_manager_cls_gated(rerank_map={"factual": True}, rerank_enabled=False)
    seen = {}
    real = mgr.genome.query_docs_ann

    def spy(*a, **kw):
        seen["override"] = kw.get("rerank_override")
        return real(*a, **kw)

    monkeypatch.setattr(mgr.genome, "query_docs_ann", spy)
    try:
        mgr.build_context("what is the capital of France", read_only=True)   # classifies factual
    finally:
        mgr.close()
    assert seen["override"] is True


def test_manager_unmapped_class_falls_back_to_global_rerank_flag(monkeypatch, seeded_manager_cls_gated):
    """``hello there`` classes ``default``, which the map does not cover, so
    the resolved override is None -> the store keeps its own global
    ``rerank_enabled`` (byte-identical fallback, mirrors the combinator's
    unmapped-class behavior)."""
    mgr = seeded_manager_cls_gated(rerank_map={"factual": True}, rerank_enabled=False)
    seen = {}
    real = mgr.genome.query_docs_ann

    def spy(*a, **kw):
        seen["override"] = kw.get("rerank_override")
        return real(*a, **kw)

    monkeypatch.setattr(mgr.genome, "query_docs_ann", spy)
    try:
        mgr.build_context("hello there", read_only=True)
    finally:
        mgr.close()
    assert seen["override"] is None


def test_manager_classifier_disabled_falls_back_to_global_rerank_flag(monkeypatch, seeded_manager_cls_gated):
    """Classifier disabled => classifier_result is None => resolve_class_flag
    resolves None regardless of the map, so every call sees no override."""
    mgr = seeded_manager_cls_gated(
        rerank_map={"factual": True}, rerank_enabled=False, classifier_enabled=False,
    )
    seen = {}
    real = mgr.genome.query_docs_ann

    def spy(*a, **kw):
        seen["override"] = kw.get("rerank_override")
        return real(*a, **kw)

    monkeypatch.setattr(mgr.genome, "query_docs_ann", spy)
    try:
        mgr.build_context("what is the capital of France", read_only=True)
    finally:
        mgr.close()
    assert seen["override"] is None


def test_manager_dense_off_path_passes_query_text_into_query_docs(monkeypatch, seeded_manager_cls_gated):
    """The dense-off branch of ``_retrieve`` must pass ``query_text=<the raw
    query>`` into ``query_docs`` — Task 5's lex-path rerank seam only fires
    when ``query_text`` is supplied, so withholding it would silently keep
    the seam inert even with rerank enabled."""
    mgr = seeded_manager_cls_gated(
        rerank_map={}, rerank_enabled=False, dense_embedding_enabled=False,
    )
    seen = {}
    real = mgr.genome.query_docs

    def spy(*a, **kw):
        seen["query_text"] = kw.get("query_text")
        return real(*a, **kw)

    monkeypatch.setattr(mgr.genome, "query_docs", spy)
    try:
        mgr.build_context("what is the capital of France", read_only=True)
    finally:
        mgr.close()
    assert seen["query_text"] == "what is the capital of France"


# ═══ 9. Task 7 — dead blend rerank branch deleted ════════════════════════
#
# Tasks 5-6 landed the pre-cap cross-encoder seam inside KnowledgeStore
# (sections 1-8 above). This left the OLD post-cap rerank branch in
# ``scoring/blend.py::apply_candidate_refiners`` dead: it was gated on
# ``hasattr(ribosome, "rerank")``, which ``DeBERTaRibosome`` never satisfies
# (it only defines ``re_rank``) and which misroutes to the LLM
# ``Compressor.rerank`` for ollama/litellm backends. Task 7 deletes that
# branch and the ``allow_rerank``/``rerank_enabled``/``ribosome`` params.


def test_blend_cap_is_unconditional_truncation():
    import inspect
    from cymatix_context.scoring.blend import apply_candidate_refiners
    sig = inspect.signature(apply_candidate_refiners)
    for gone in ("allow_rerank", "rerank_enabled", "ribosome"):
        assert gone not in sig.parameters
    src = inspect.getsource(apply_candidate_refiners)
    assert 'hasattr' not in src.split("def ", 1)[-1] or "rerank" not in src


# ═══ 10. ``_rerank_doc_text`` pair form (#341 sanctioned variation, 829k A/Bs) ═
#
# The 829k A/Bs landed under the recall bar, so the pre-sanctioned variation
# ships: mirror ``DeBERTaRibosome.re_rank``'s pair text exactly --
# ``f"{summary} [{domains}]"`` -- instead of scoring raw ``content``. A
# summary-less document (fragment stub, summary-only ingest gone the other
# way) degrades gracefully back to the old content-based form rather than
# scoring a garbage " []" pair.


def test_rerank_doc_text_uses_summary_and_domains_when_summary_present():
    doc = _make_gene("body text nobody should see", gene_id="g-summary")
    doc.promoter.summary = "a concise summary of the document"
    doc.promoter.domains = ["alpha", "beta"]
    assert _rerank_doc_text(doc) == "a concise summary of the document [alpha, beta]"


def test_rerank_doc_text_falls_back_to_content_when_summary_missing():
    doc = _make_gene("the body text is what gets scored", gene_id="g-no-summary")
    assert doc.promoter.summary == ""          # default -> no summary
    assert _rerank_doc_text(doc) == "the body text is what gets scored"
