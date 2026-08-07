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
from cymatix_context.knowledge_store import KnowledgeStore
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


def _make_gene(content: str, *, gene_id: str) -> Gene:
    return Gene(
        gene_id=gene_id,
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=["topic"], entities=[]),
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
        store.upsert_gene(_make_gene(content, gene_id=_gid(i)), apply_gate=False)


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
