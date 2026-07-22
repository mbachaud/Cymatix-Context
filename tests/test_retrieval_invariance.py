"""Scoring-invariance audit — integration-level tests over the full
``query_docs`` path (2026-07-08 symmetry audit).

Principle under test: retrieval DECISIONS must survive strictly-monotone
rescaling of raw score magnitudes.  Absolute scales are legitimate only
when explicit + calibrated + re-fittable.  Two live defects were found
and are PINNED (not fixed) here:

DEFECT-1  Under ``fusion_mode="rrf"`` (the config default), the final
          score is ``fused + rerank_additive`` (knowledge_store.py:2928-2931).
          Fused RRF scale is O(0.05-0.5) (``weight/(k+rank)``, k=60) while
          the re-rank additives carry additive-scale authority bonuses
          (+2.0 source-path, +1.5 domain-primacy, +0.5 recency at
          knowledge_store.py:1562/1570/1580), party +0.5, access-rate
          <=0.25 and the warm sema_boost.  One authority hit is ~40x the
          fused signal — the bonuses effectively BECOME the ranking.

DEFECT-2  FIXED in #256: ``KnowledgeStore.__init__`` now defaults
          ``fusion_mode="rrf"``, agreeing with ``RetrievalConfig``
          (config.py:461); ``test_layer_defaults_agree`` guards the
          equality permanently.
          NOTE: every store constructed directly in this file still
          passes ``fusion_mode=...`` EXPLICITLY (self-documenting).

Companion unit-level tests: tests/test_fusion_invariance.py.
Audit doc: docs/research/2026-07-08-scoring-invariance-audit.md.
Reused harness: tests/test_additive_weight_plumbing.py.
"""
from __future__ import annotations

import contextlib
import inspect
import math
import time

import pytest

from cymatix_context.config import AbstainClassFloors, RetrievalConfig
from cymatix_context.genome import Genome
from cymatix_context.knowledge_store import KnowledgeStore
from cymatix_context.pipeline.tier_logic import apply_budget_tiers
from cymatix_context.retrieval import fusion as fusion_mod
from cymatix_context.schemas import (
    ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
)
from tests.test_additive_weight_plumbing import (
    make_genome as harness_make_genome,
    run_query as harness_run_query,
)

# ─── Local corpus helpers ─────────────────────────────────────────────
#
# The harness ``build_corpus`` deliberately fires the authority tier
# (domain-primacy on gA/gC, +0.5 recency on every fresh doc).  The
# invariance tests below need a rerank-FREE corpus slice (DEFECT-1
# pollutes final_scores otherwise), so we build our own docs with:
#   - created_at 30 days in the past  -> no +0.5 recency authority
#   - the query term at domain index >= 3 -> exact-tag hit WITHOUT the
#     +1.5 domain-primacy boost (which checks only promoter.domains[:3])
#   - no query term in source_id      -> no +2.0 source authority
#   - no party_id / no recent_accesses -> no party / access-rate bonus

_OLD_TS = time.time() - 30 * 86400  # 30 days ago (recency window is 48h)

# 12-token content pad (query term "alpha" never appears here and no pad
# word is "alpha"-prefixed, so pads never hit FTS/tag/prefix tiers).
_PAD = [
    "quartz", "basalt", "gneiss", "schist", "marble", "slate",
    "granite", "pumice", "obsidian", "shale", "chert", "flint",
]


def _content(alpha_mentions: int) -> str:
    """Exactly 12 tokens: N x 'alpha' + pad.  Equal document length across
    docs means SQLite BM25's avgdl normalization shifts every hit doc's
    denominator identically, so tf ordering is corpus-size-stable."""
    assert 0 <= alpha_mentions <= 12
    return " ".join(["alpha"] * alpha_mentions + _PAD[: 12 - alpha_mentions])


def _doc(
    gid: str,
    content: str,
    domains: list[str],
    *,
    source_id: str | None = None,
    created_at: float | None = _OLD_TS,
) -> Gene:
    epi = (
        EpigeneticMarkers()
        if created_at is None
        else EpigeneticMarkers(created_at=created_at, last_accessed=created_at)
    )
    return Gene(
        gene_id=gid,
        content=content,
        complement="",
        codons=[],
        promoter=PromoterTags(domains=list(domains), entities=[]),
        epigenetics=epi,
        chromatin=ChromatinState.OPEN,
        is_fragment=False,
        source_id=source_id,
    )


def _fillers(n: int, start: int = 0) -> list[Gene]:
    """Inert docs: unique non-matching domains, no 'alpha' token anywhere.
    They keep the FTS BM25 idf for 'alpha' positive (SQLite's
    Robertson-Sparck-Jones idf goes negative when a term appears in more
    than half the corpus) and serve as the dilution mass for the
    corpus-scale test."""
    docs = []
    for i in range(start, start + n):
        words = " ".join(f"fill{i}w{j}" for j in range(12))
        docs.append(_doc(f"filler{i:04d}", words, [f"fillerdom{i}"]))
    return docs


def _new_store(docs: list[Gene], **genome_kwargs) -> Genome:
    g = Genome(path=":memory:", **genome_kwargs)
    for d in docs:
        g.upsert_gene(d, apply_gate=False)
    g.conn.commit()
    return g


def _run(g: Genome):
    """Run the canonical single-term query; return (ranked_ids, scores,
    tier_contrib) — mirrors the harness run_query shape."""
    genes = g.query_genes(
        domains=["alpha"], entities=[], max_genes=12, read_only=True,
    )
    ranked = [x.gene_id for x in genes]
    scores = dict(g.last_query_scores)
    contrib = {gid: dict(t) for gid, t in g.last_tier_contributions.items()}
    return ranked, scores, contrib


def _invariance_corpus() -> list[Gene]:
    """4 hit docs with DIFFERENT per-tier rank profiles + 8 inert fillers.

    hit tiers per doc ('alpha' always at domain index 3 -> no primacy):
      inv_a — exact tag + prefix + fts (3 mentions) + lex_anchor
      inv_b — exact tag + prefix + fts (1 mention) + lex_anchor
      inv_c — prefix tag ('alphaville') + fts (2 mentions)
      inv_d — fts only (4 mentions; best fts rank, no tags)
    """
    return [
        _doc("inv_a", _content(3), ["w1", "w2", "w3", "alpha"]),
        _doc("inv_b", _content(1), ["w4", "w5", "w6", "alpha"]),
        _doc("inv_c", _content(2), ["w7", "w8", "w9", "alphaville"]),
        _doc("inv_d", _content(4), ["w10", "w11", "w12"]),
    ] + _fillers(8)


# Fused-tier names a rerank-free corpus is allowed to fire.  Anything
# else (authority / party_attr / access_rate / sema_boost) means the
# rerank additives leaked into what should be a pure-fusion fixture.
_FUSED_ONLY_TIERS = {"pki", "tag_exact", "tag_prefix", "fts5", "lex_anchor"}


# ─── Monotone per-tier rescale machinery (RRF invariance) ─────────────
#
# knowledge_store.query_docs imports the Fuser LOCALLY
# (``from .retrieval.fusion import Fuser as _Fuser``, knowledge_store.py:2012),
# resolving the attribute at call time — so patching
# cymatix_context.retrieval.fusion.Fuser intercepts every tier feed.

_TIER_TRANSFORMS = {
    "tag_exact": lambda s: 250.0 * s + 17.0,   # affine
    "tag_prefix": lambda s: math.exp(s),       # exponential
    "fts5": lambda s: s ** 3,                  # odd cube (monotone on R)
    "lex_anchor": lambda s: math.log1p(s),     # log1p (monotone on s > -1)
}


def _default_transform(s: float) -> float:
    return math.atan(s)  # bounded, strictly increasing on R


class _MonotoneFuser(fusion_mod.Fuser):
    """Fuser that applies a per-tier strictly-monotone transform to every
    raw score BEFORE delegating.  RRF operates on ranks; a strictly
    increasing map preserves strict order and maps ties to ties, so the
    fused output must be bitwise identical.

    ``tiers_transformed`` (class-level) counts add_tier interceptions so
    the test can assert the patch seam was actually exercised — without
    it, a refactor that stops resolving ``fusion.Fuser`` at call time
    would make the invariance assertion pass vacuously.
    """

    tiers_transformed = 0  # plain class attr (NOT a dataclass field)

    def add_tier(self, tier_name, ranked_ids, weight=1.0):
        type(self).tiers_transformed += 1
        f = _TIER_TRANSFORMS.get(tier_name, _default_transform)
        transformed = [(gid, f(float(score))) for gid, score in ranked_ids]
        super().add_tier(tier_name, transformed, weight=weight)


# ─── SPLADE seam patch (raw tier-score injection, both modes) ─────────


@contextlib.contextmanager
def _patched_splade(hits):
    """Replace the SPLADE model calls with fixed hits (local variant of
    the harness patched_splade — we need custom (gid, raw) pairs)."""
    from cymatix_context.backends import splade_backend

    old_encode = splade_backend.encode
    old_query = splade_backend.query_splade
    splade_backend.encode = lambda text, **kw: {}
    splade_backend.query_splade = lambda conn, sparse, limit=20: list(hits)
    try:
        yield
    finally:
        splade_backend.encode = old_encode
        splade_backend.query_splade = old_query


# ═══ INVARIANTS (rrf mode) ════════════════════════════════════════════


def test_query_docs_order_invariant_under_monotone_tier_rescale(monkeypatch):
    """RRF-mode end-to-end ranking is invariant under per-tier strictly
    monotone rescaling of raw tier scores.

    Uses a rerank-free corpus (no authority/party/access-rate/sema hits)
    because DEFECT-1 otherwise mixes additive-scale bonuses into
    final_scores, which are NOT covered by the rank-fusion symmetry.
    """
    g = _new_store(_invariance_corpus(), fusion_mode="rrf")
    try:
        base_ranked, base_scores, base_contrib = _run(g)

        # Fixture guard: the corpus must be rerank-free and fire multiple
        # tiers with different per-doc profiles.
        assert set(base_ranked) == {"inv_a", "inv_b", "inv_c", "inv_d"}
        for gid, tiers in base_contrib.items():
            leaked = set(tiers) - _FUSED_ONLY_TIERS
            assert not leaked, f"rerank additives leaked into fixture: {gid} -> {leaked}"
        fired = {t for tiers in base_contrib.values() for t in tiers}
        assert {"tag_exact", "tag_prefix", "fts5", "lex_anchor"} <= fired

        # Patch the module attribute the local import resolves at call time.
        monkeypatch.setattr(fusion_mod, "Fuser", _MonotoneFuser)
        _MonotoneFuser.tiers_transformed = 0
        patched_ranked, patched_scores, _ = _run(g)

        # Liveness guard: the patched Fuser must actually have intercepted
        # tier feeds (a dead seam would pass the equality checks vacuously).
        assert _MonotoneFuser.tiers_transformed >= 4

        assert patched_ranked == base_ranked, (
            "RRF ordering changed under a strictly-monotone per-tier "
            f"rescale:\n  base    {base_ranked}\n  patched {patched_ranked}"
        )
        # Ranks unchanged -> identical 1/(k+rank) arithmetic -> the fused
        # scores must be bitwise identical, not merely order-equal.
        assert patched_scores == base_scores
    finally:
        g.close()


def test_corpus_scale_preserves_relative_order():
    """Adding inert filler docs (no query-term overlap) preserves the
    relative order of the original hit set under RRF.

    BOUNDED CLAIM — this is not a general anti-dilution guarantee:
      * fillers share NO query terms (they enter no tier's ranked list);
      * the query is single-term, so FTS idf shifts are a uniform
        multiplier across hits;
      * all hit docs have equal token length, so BM25's avgdl shift is
        order-preserving within the fts tier.
    Under those conditions every tier's internal ranking of the original
    hits is unchanged, hence so is the fused order.
    """
    g = _new_store(_invariance_corpus(), fusion_mode="rrf")
    try:
        base_ranked, _, _ = _run(g)
        base_ids = set(base_ranked)
        assert base_ids == {"inv_a", "inv_b", "inv_c", "inv_d"}

        for d in _fillers(40, start=100):
            g.upsert_gene(d, apply_gate=False)
        g.conn.commit()

        grown_ranked, _, _ = _run(g)
        # No filler may enter the candidate set at all…
        assert not [gid for gid in grown_ranked if gid.startswith("filler")]
        # …and the original hits keep their relative order.
        assert [gid for gid in grown_ranked if gid in base_ids] == base_ranked
    finally:
        g.close()


# ═══ CHARACTERIZATION (pin CURRENT behavior — audit findings) ═════════


def test_defect1_authority_bonus_dominates_rrf_ordering():
    # Pins DEFECT-1 (authority +2.0 rerank additive at knowledge_store.py:1562
    # dwarfs the O(0.15) fused RRF scale, knowledge_store.py:2928-2931) — see
    # docs/research/2026-07-08-scoring-invariance-audit.md; a fix PR must
    # consciously flip this.
    """Doc A ranks strictly better than doc B in EVERY fused tier, but B's
    source_id contains the query term ('alpha' in 'notes/alpha_setup.md'
    -> +2.0 source-authority bonus).  Today B outranks A: one authority
    hit is ~13x the entire fused budget of a 4-tier sweep (9/61 ~ 0.148).
    """
    docs = [
        # A: better fts rank (3 mentions vs 1, equal length), and wins all
        # tag/lex tie-breaks via gene_id asc ("auth_a" < "auth_b").
        _doc("auth_a", _content(3), ["wq1", "wq2", "wq3", "alpha"]),
        # B: strictly worse in every fused tier; carries the source-path
        # authority trigger. 'alpha' at domain index 3 -> NO +1.5 primacy,
        # created_at 30d ago -> NO +0.5 recency, so authority == 2.0 exactly.
        _doc("auth_b", _content(1), ["wr1", "wr2", "wr3", "alpha"],
             source_id="notes/alpha_setup.md"),
    ] + _fillers(4, start=200)

    g = _new_store(docs, fusion_mode="rrf")
    try:
        ranked, scores, contrib = _run(g)
    finally:
        g.close()

    # The authority trigger fired on B only, at exactly the source bonus,
    # and is B's ONLY rerank additive — if another additive (access_rate,
    # sema_boost, party) ever co-fires here, the fused_b arithmetic below
    # would be inflated; fail loudly instead of drifting.
    assert contrib["auth_b"].get("authority") == 2.0
    assert set(contrib["auth_b"]) - _FUSED_ONLY_TIERS == {"authority"}
    assert "authority" not in contrib.get("auth_a", {})
    # A carries no rerank additives -> scores["auth_a"] IS its fused score.
    assert set(contrib["auth_a"]) <= _FUSED_ONLY_TIERS

    # A is strictly better on the fused (rank-symmetric) signal…
    fused_b = scores["auth_b"] - contrib["auth_b"]["authority"]
    assert scores["auth_a"] > fused_b

    # …yet TODAY the additive-scale bonus decides the ranking outright.
    assert ranked.index("auth_b") < ranked.index("auth_a")
    assert scores["auth_b"] > 10 * scores["auth_a"], (
        "expected the +2.0 authority bonus to dwarf the fused scale "
        f"(B={scores['auth_b']:.4f}, A={scores['auth_a']:.4f})"
    )


def test_layer_defaults_agree():
    # DEFECT-2 fixed in #256: KnowledgeStore.__init__ and RetrievalConfig
    # now share one fusion default. This is the permanent single-source
    # guard — assert EQUALITY with the config layer, not a literal, so
    # any future default move must touch both layers in the same PR.
    config_default = RetrievalConfig().fusion_mode

    sig_default = inspect.signature(
        KnowledgeStore.__init__
    ).parameters["fusion_mode"].default
    assert sig_default == config_default

    ks = KnowledgeStore(path=":memory:")
    try:
        assert ks._fusion_mode == config_default
    finally:
        ks.close()

    # And the shipped default is RRF (the #247 flip, SIKE Run-2 receipts).
    assert config_default == "rrf"


def test_additive_mode_not_rescale_invariant():
    # Pins that additive fusion is magnitude-coupled (gene_scores +=
    # raw tier value, knowledge_store.py:2955-2957) so a non-affine
    # strictly-monotone rescale of ONE tier's raw scores flips the final
    # ordering — see docs/research/2026-07-08-scoring-invariance-audit.md;
    # a fix PR must consciously flip this.
    """Under fusion_mode="additive" the Fuser is built but never queried
    (knowledge_store.py:2955-2957), so the _MonotoneFuser patch used in
    the RRF invariance test is provably inert here — itself evidence the
    additive path never enters rank space.  Instead we apply the monotone
    transform at the SPLADE seam (raw (gid, score) pairs feeding both the
    additive accumulator and the Fuser):

        f(x) = sqrt(20*x)   — strictly increasing, non-affine

    Corpus: flip_c is splade-heavy (raw 18 -> additive 3.15), flip_d is
    splade-light + tag_prefix (raw 6 -> 1.05 + 1.5 = 2.55).  Compressing
    the splade magnitudes shrinks flip_c's edge below flip_d's tag term:
    transformed additive gives flip_c 3.320 vs flip_d 3.417 -> ORDER FLIPS.
    The same seam transform under RRF leaves the ordering unchanged
    (within-tier ranks are preserved), pinning the asymmetry.
    """
    raw_base = [("flip_c", 18.0), ("flip_d", 6.0)]

    def f(x: float) -> float:
        return math.sqrt(20.0 * x)

    raw_tx = [(gid, f(s)) for gid, s in raw_base]
    # Transform sanity: strictly monotone -> within-tier order preserved.
    assert f(6.0) < f(18.0)
    assert (
        sorted((gid for gid, s in raw_base), key=dict(raw_base).get)
        == sorted((gid for gid, s in raw_tx), key=dict(raw_tx).get)
    )

    def _mk(mode: str) -> Genome:
        docs = [
            _doc("flip_c", " ".join(_PAD), ["cyd1", "cyd2", "cyd3"]),
            _doc("flip_d", " ".join(f"dor{j}w" for j in range(12)),
                 ["dor1", "dor2", "dor3", "alphabeta"]),
        ]
        with _patched_splade([]):  # ingest-time encode patch only
            return _new_store(docs, fusion_mode=mode, splade_enabled=True)

    # ── additive: the rescale flips the ranking ──
    g_add = _mk("additive")
    try:
        with _patched_splade(raw_base):
            base_ranked, base_scores, _ = _run(g_add)
        with _patched_splade(raw_tx):
            tx_ranked, tx_scores, _ = _run(g_add)
    finally:
        g_add.close()

    assert base_ranked == ["flip_c", "flip_d"]
    assert base_scores["flip_c"] == pytest.approx(3.15)
    assert base_scores["flip_d"] == pytest.approx(2.55)
    assert tx_ranked == ["flip_d", "flip_c"]
    assert tx_ranked != base_ranked  # the pinned non-invariance

    # ── rrf: the SAME seam transform leaves the ordering unchanged ──
    g_rrf = _mk("rrf")
    try:
        with _patched_splade(raw_base):
            rrf_base_ranked, _, _ = _run(g_rrf)
        with _patched_splade(raw_tx):
            rrf_tx_ranked, _, _ = _run(g_rrf)
    finally:
        g_rrf.close()
    assert rrf_tx_ranked == rrf_base_ranked == ["flip_d", "flip_c"]


def test_abstain_floors_bypassed_under_rrf():
    # Pins the fusion_mode fork in pipeline/tier_logic.py (skip_absolute_floors
    # at :113 gates the abstain absolute floor at :179; RRF runs the
    # baseline-normalized ratio gate only, :146-156) — see
    # docs/research/2026-07-08-scoring-invariance-audit.md; a fix PR must
    # consciously flip this.
    """Identical low-magnitude score profile through apply_budget_tiers:

      additive  -> ABSTAIN  (top 0.30 < abstain_top 2.5 AND ratio 1.6 < 1.8)
      rrf       -> BROAD    (absolute floor bypassed; normalized ratio
                             (0.30-0.10)/(0.1875-0.10) ~ 2.286 >= 1.5)

    and the additive decision is MAGNITUDE-COUPLED: scaling every score
    x1000 flips additive from abstain to broad (top 300 clears the 2.5
    floor; the ratio never changed), while rrf returns the same decision
    at both scales — its gate is scale-invariant by construction.
    """
    cands = [_doc(f"tl{i}", "tier logic candidate", ["dom"]) for i in range(4)]
    profile = {"tl0": 0.30, "tl1": 0.20, "tl2": 0.15, "tl3": 0.10}
    scaled = {gid: s * 1000.0 for gid, s in profile.items()}
    floors = AbstainClassFloors()  # defaults: abstain 2.5 / focused 2.5 / tight 5.0

    add_small = apply_budget_tiers(cands, profile, floors, fusion_mode="additive")
    rrf_small = apply_budget_tiers(cands, profile, floors, fusion_mode="rrf")
    add_big = apply_budget_tiers(cands, scaled, floors, fusion_mode="additive")
    rrf_big = apply_budget_tiers(cands, scaled, floors, fusion_mode="rrf")

    # The exact observable difference on the SAME profile:
    assert add_small.abstain is True
    assert add_small.abstain_top_score == pytest.approx(0.30)
    assert add_small.abstain_ratio == pytest.approx(1.6)  # legacy top/mean
    assert rrf_small.abstain is False
    assert rrf_small.budget_tier == "broad"
    assert len(rrf_small.candidates) == 4

    # Additive flips on pure magnitude (absolute floor); rrf is invariant.
    assert add_big.abstain is False
    assert add_big.budget_tier == "broad"
    assert rrf_big.abstain is False
    assert rrf_big.budget_tier == "broad"


def test_sema_boost_damping_inert_under_rrf():
    # Pins the sema_boost damping formula boost_scale = max(0.5, 1 - top/40)
    # (knowledge_store.py:2429-2432): its constants are additive-calibrated,
    # so at RRF fused magnitudes the damping never engages and the boost
    # lands on final_scores at ~an order of magnitude above the entire
    # fused budget — see docs/research/2026-07-08-scoring-invariance-audit.md;
    # a fix PR must consciously flip this.
    """Two-part pin (the formula constants are inline literals, so part A
    replicates them with a source-line reference rather than importing):

    A) Formula: for tops on the RRF final-score scale (<= ~0.5, since a
       doc ranked #1 in every fused tier tops out near sum(weights)/(k+1)
       ~ 0.4), boost_scale stays >= 0.9875 — within 1.25% of undamped.
       The 0.5 floor is reached exactly at top == 20.0, which is also the
       tier's own entry gate (top_score < 20.0, knowledge_store.py:2402):
       inside the gate the damping factor lives in (0.5, 1.0] and can
       never counteract the ~40x fused/additive scale mismatch.

    B) Code path (harness corpus, fusion_mode="rrf"): gA's sema_boost
       tier contribution — a rerank additive summed into final_scores at
       knowledge_store.py:2928-2931 — exceeds the maximum fused score ANY
       document could achieve (rank #1 in every tier).
       NOTE: gene_scores dual-writes identically in both modes, so
       top_score at the sema stage is additive-scale even under rrf; the
       damping is computed against a scale the RRF output never sees.
    """
    # ── A: formula replication (knowledge_store.py:2429) ──
    # Drift guard: part A replicates inline literals, so pin them to the
    # actual source — if the constants change upstream, this test breaks
    # instead of silently validating its own copy.
    ks_source = inspect.getsource(KnowledgeStore)
    assert "boost_scale = max(0.5, 1.0 - top_score / 40.0)" in ks_source
    assert "top_score < 20.0" in ks_source

    def boost_scale(top: float) -> float:
        return max(0.5, 1.0 - top / 40.0)

    for rrf_scale_top in (0.0, 0.05, 0.1475, 0.295, 0.5):
        assert boost_scale(rrf_scale_top) >= 0.9875
    assert boost_scale(20.0) == 0.5      # floor met exactly at the entry gate
    assert boost_scale(19.0) > 0.5       # never reached inside the gate

    # ── B: code path ──
    g = harness_make_genome(fusion_mode="rrf")
    try:
        _ranked, _scores, contrib = harness_run_query(g)
    finally:
        g.close()

    sema_boost = contrib["gA"]["sema_boost"]
    # Generous over-bound on any doc's total fused score: sum of ALL
    # default tier weights (pki 1.0 + filename 4.0 + tag_exact 3.0 +
    # tag_prefix 1.5 + fts5 3.0 + splade 3.5 + dense 1.0 + sema_cold 3.0 +
    # lex_anchor 1.5 + harmonic 1.0 + entity_graph 0.5 + sr 1.5 ~ 24.5,
    # padded to 30) / (k + 1).
    max_possible_fused = 30.0 / 61.0
    assert sema_boost > max_possible_fused, (
        "sema_boost rerank additive no longer dominates the fused scale "
        f"({sema_boost:.4f} <= {max_possible_fused:.4f})"
    )

    # Recover the damping factor actually applied: sema_boost = sim * 2.0
    # * boost_scale, with gA's cosine fixed by the harness FakeSemaCodec
    # (_vec(0.75, 0.5) vs the unit query vec).
    sim_gA = 0.75 / math.hypot(0.75, 0.5)
    observed_scale = sema_boost / (2.0 * sim_gA)
    assert 0.5 <= observed_scale <= 1.0
