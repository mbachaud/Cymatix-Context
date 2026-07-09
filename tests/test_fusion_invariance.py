"""Scoring-invariance audit (2026-07-08): metamorphic tests for the Fuser.

``helix_context/retrieval/fusion.py`` (module docstring lines 10-12, 23)
claims RRF "operates on ranks, which are scale-invariant" and that the
tie-break "makes the test plan §10 deterministic" — but no test asserted
either property until now.

The metamorphic relation under test: for any strictly-monotone,
tie-preserving map ``m_t`` applied independently to each tier's raw
scores, per-tier ranks are unchanged, therefore the fused ordering (and
the fused magnitudes) must be unchanged. Absolute fused magnitudes may
legitimately move only under uniform weight scaling (linearly) and
under ``k`` changes (non-linearly) — never the ordering.

Companion file: ``tests/test_retrieval_invariance.py`` audits the same
symmetry at the ``query_docs`` / knowledge-store layer, where two live
defects (additive authority bonuses on top of RRF-scale fused scores;
layer-default disagreement on ``fusion_mode``) break it.
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Sequence, Tuple

import pytest

from helix_context.retrieval.fusion import DEFAULT_RRF_K, Fuser, rank_by_score


# ─── Fixture: multi-tier score table ──────────────────────────────────
#
# 5 tiers, 14 gene ids, asymmetric overlaps (g11 dense-only, g12
# tag-only, g13 harmonic-only; g01 appears in three tiers), and raw-score
# ties inside four of the five tiers. Score scales deliberately mimic the
# non-commensurate production tiers (negative bm25, cosine, tag 3.0,
# harmonic small-int, filename 4.0) that motivated RRF in the first place.

TierTable = List[Tuple[str, float, List[Tuple[str, float]]]]

BASE_TIERS: TierTable = [
    # (tier_name, weight, [(gene_id, raw_score), ...])
    ("fts5", 3.0, [
        ("g01", -1.2),
        ("g02", -2.5), ("g03", -2.5),          # tie
        ("g04", -3.1),
        ("g05", -4.0),
        ("g06", -5.5),
        ("g07", -6.0),
    ]),
    ("dense", 1.0, [
        ("g03", 0.91),
        ("g08", 0.88),
        ("g01", 0.75), ("g09", 0.75),          # tie
        ("g10", 0.60),
        ("g05", 0.42),
        ("g11", 0.30),
    ]),
    ("tag_exact", 3.0, [
        ("g02", 3.0), ("g08", 3.0), ("g12", 3.0), ("g04", 3.0),  # all tied
    ]),
    ("harmonic", 1.5, [
        ("g10", 3.0),
        ("g01", 2.0), ("g13", 2.0),            # tie
        ("g06", 1.0), ("g14", 1.0),            # tie
        ("g09", 0.5),
    ]),
    ("filename_anchor", 4.0, [
        ("g14", 4.0), ("g07", 4.0),            # tie
    ]),
]

ALL_GENE_IDS = {f"g{i:02d}" for i in range(1, 15)}


# ─── Helpers ──────────────────────────────────────────────────────────


def _fuse(tiers: TierTable, k: int = DEFAULT_RRF_K) -> Fuser:
    f = Fuser(k=k)
    for name, weight, pairs in tiers:
        f.add_tier(name, pairs, weight=weight)
    return f


def _topk_order(f: Fuser) -> List[str]:
    """Full ordering as top_k sees it."""
    return [gid for gid, _ in f.top_k(len(f))]


def _all_scores_order(f: Fuser) -> List[str]:
    """Ordering induced by all_scores() under the (-score, gid) rule."""
    return [
        gid
        for gid, _ in sorted(
            f.all_scores().items(), key=lambda kv: (-kv[1], kv[0])
        )
    ]


def _apply_pointwise(
    tiers: TierTable,
    fn_by_tier: Dict[str, Callable[[float], float]],
) -> TierTable:
    """Apply an independent pointwise map to each tier's raw scores."""
    return [
        (name, weight, [(gid, fn_by_tier[name](s)) for gid, s in pairs])
        for name, weight, pairs in tiers
    ]


def _dense_rank_remap(pairs: Sequence[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """Replace scores by their dense ascending rank index.

    Strictly order-preserving and tie-preserving: equal scores map to
    equal values, distinct scores keep their relative order. This is
    the 'enumerate ranks reversed' arbitrary monotone map — it throws
    away every property of the raw scale except the order.
    """
    lut = {s: float(i) for i, s in enumerate(sorted({s for _, s in pairs}))}
    return [(gid, lut[s]) for gid, s in pairs]


_SPREAD = [-9.9e6, -1234.5, -1.0, 0.0, 1e-9, 0.5, 3.14159, 42.0, 9000.0, 7.7e8]


def _spread_remap(pairs: Sequence[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """Map each unique score to a wildly-spaced arbitrary value.

    Same order, same ties, absurdly different magnitudes and gaps.
    """
    uniq = sorted({s for _, s in pairs})
    assert len(uniq) <= len(_SPREAD), "fixture grew past the spread table"
    lut = {s: _SPREAD[i] for i, s in enumerate(uniq)}
    return [(gid, lut[s]) for gid, s in pairs]


def _shift_cube(pairs: Sequence[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """Positive-shift then cube: non-linear, strictly monotone."""
    lo = min(s for _, s in pairs)
    return [(gid, (s - lo + 1.0) ** 3) for gid, s in pairs]


def _baseline() -> Tuple[List[str], Dict[str, float]]:
    f = _fuse(BASE_TIERS)
    order = _topk_order(f)
    # Fixture sanity: every gene id fused, orderings self-consistent.
    assert set(order) == ALL_GENE_IDS
    assert order == _all_scores_order(f)
    return order, f.all_scores()


def _assert_same_ordering(mapped_tiers: TierTable, label: str) -> None:
    base_order, base_scores = _baseline()
    f = _fuse(mapped_tiers)
    assert _topk_order(f) == base_order, (
        f"{label}: top_k ordering drifted under a rank-preserving rescale"
    )
    assert _all_scores_order(f) == base_order, (
        f"{label}: all_scores()-induced ordering drifted under rescale"
    )
    # Ranks are identical, so the fused magnitudes must be identical
    # too — RRF never sees the raw scale, only the ranks.
    assert f.all_scores() == pytest.approx(base_scores), (
        f"{label}: fused magnitudes moved — raw scale leaked into RRF"
    )


# ─── 1. affine rescale per tier ───────────────────────────────────────


def test_order_invariant_under_affine_rescale_per_tier():
    """a*x + b (a > 0), independent per tier, must be a no-op.

    Coefficients are deliberately mismatched across tiers — this is
    exactly the 'one over-scaled tier dominates' failure mode the
    additive accumulator had (fusion.py docstring) and RRF must not.
    """
    affine = {
        "fts5": lambda x: 3.7 * x + 12.0,
        "dense": lambda x: 0.01 * x - 5.0,
        "tag_exact": lambda x: 250.0 * x,
        "harmonic": lambda x: 0.001 * x + 1000.0,
        "filename_anchor": lambda x: 42.0 * x - 7.0,
    }
    _assert_same_ordering(
        _apply_pointwise(BASE_TIERS, affine), "affine per-tier"
    )


# ─── 2. exponential / non-linear monotone rescale per tier ────────────


def test_order_invariant_under_exp_rescale_per_tier():
    """exp(x) per tier, then a second non-linear map (shift-cube)."""
    exp_maps = {name: math.exp for name, _, _ in BASE_TIERS}
    _assert_same_ordering(
        _apply_pointwise(BASE_TIERS, exp_maps), "exp per-tier"
    )

    cubed = [
        (name, weight, _shift_cube(pairs))
        for name, weight, pairs in BASE_TIERS
    ]
    _assert_same_ordering(cubed, "shift-cube per-tier")


# ─── 3. arbitrary monotone maps ───────────────────────────────────────


def test_order_invariant_under_arbitrary_monotone_maps():
    """Any tie-preserving strict-order-preserving relabeling is a no-op.

    Dense-rank remap discards everything but the order; spread remap
    additionally scrambles magnitudes and gaps across nine decades.
    """
    ranked = [
        (name, weight, _dense_rank_remap(pairs))
        for name, weight, pairs in BASE_TIERS
    ]
    _assert_same_ordering(ranked, "dense-rank remap")

    spread = [
        (name, weight, _spread_remap(pairs))
        for name, weight, pairs in BASE_TIERS
    ]
    _assert_same_ordering(spread, "spread remap")


# ─── 4. uniform weight scaling ────────────────────────────────────────


def test_order_invariant_under_uniform_weight_scaling():
    """Multiplying ALL tier weights by c > 0 scales every fused score
    by exactly c and changes nothing about the ordering.

    This is the legitimate global degree of freedom: fused scores are
    linear in the weight vector, so a uniform rescale is a pure change
    of units. Any downstream consumer that adds an absolute bonus to
    these scores (see DEFECT-1 in the companion audit file) silently
    breaks this symmetry.
    """
    base_order, base_scores = _baseline()
    for c in (0.25, 7.0):
        scaled = [
            (name, weight * c, pairs) for name, weight, pairs in BASE_TIERS
        ]
        f = _fuse(scaled)
        assert _topk_order(f) == base_order, (
            f"uniform weight scale c={c}: ordering drifted"
        )
        assert _all_scores_order(f) == base_order
        got = f.all_scores()
        assert set(got) == set(base_scores)
        for gid, base_val in base_scores.items():
            assert got[gid] == pytest.approx(c * base_val, rel=1e-12), (
                f"uniform weight scale c={c}: score for {gid} is not "
                f"exactly c times baseline"
            )


# ─── 5. k moves magnitudes, never order ───────────────────────────────


def test_k_changes_magnitudes_not_order():
    """Same tiers under k in {10, 60, 240}, symmetric weights.

    Extends the idiom at tests/test_fusion_rrf.py:132-139 (k=10 sanity
    check) to a dominance-chain fixture where the ordering is provably
    k-invariant: every doc appears in all four tiers and the sorted
    per-doc rank vectors form a componentwise chain, so
    Σ 1/(k+rank) preserves the chain for every k > 0. Magnitudes,
    by contrast, must move with k.

    (Ordering under k is NOT invariant for arbitrary rank profiles —
    that is why this test uses its own fixture rather than BASE_TIERS,
    whose asymmetric overlaps make ordering legitimately k-sensitive.)
    """
    # Rank vectors (sorted): d01 (1,1,1,2) < d02 (1,2,2,2) < d03
    # (3,3,3,4) < d04 (3,4,4,4) < d05 (5,5,5,6) < d06 (5,6,6,6).
    chain_tiers: TierTable = [
        ("tier_w", 1.0, [
            ("d01", 6.0), ("d02", 5.0), ("d03", 4.0),
            ("d04", 3.0), ("d05", 2.0), ("d06", 1.0),
        ]),
        ("tier_x", 1.0, [
            ("d01", 6.0), ("d02", 5.0), ("d03", 4.0),
            ("d04", 3.0), ("d06", 2.0), ("d05", 1.0),
        ]),
        ("tier_y", 1.0, [
            ("d02", 6.0), ("d01", 5.0), ("d03", 4.0),
            ("d04", 3.0), ("d05", 2.0), ("d06", 1.0),
        ]),
        ("tier_z", 1.0, [
            ("d01", 6.0), ("d02", 5.0), ("d04", 4.0),
            ("d03", 3.0), ("d05", 2.0), ("d06", 1.0),
        ]),
    ]
    expected = ["d01", "d02", "d03", "d04", "d05", "d06"]

    scores_by_k: Dict[int, Dict[str, float]] = {}
    for k in (10, 60, 240):
        f = _fuse(chain_tiers, k=k)
        assert _topk_order(f) == expected, (
            f"k={k}: ordering moved — k must only rescale magnitudes"
        )
        scores_by_k[k] = f.all_scores()

    # Magnitudes DO change with k: every fused score strictly shrinks
    # as k grows (each contribution is w/(k+rank)).
    for gid in expected:
        assert scores_by_k[10][gid] > scores_by_k[60][gid] > scores_by_k[240][gid], (
            f"{gid}: fused magnitude did not shrink monotonically in k"
        )


# ─── 6. tie-break determinism under rescale ───────────────────────────


def test_tie_break_deterministic_under_rescale():
    """Engineered exact fused-score tie: (-score, gene_id) must break it
    the same way no matter how each tier's raw scores are rescaled.
    """
    identity = lambda x: x  # noqa: E731
    rescales = [
        (identity, identity),
        (lambda x: 1000.0 * x + 3.0, lambda x: 0.001 * x - 8.0),
        (math.exp, lambda x: (x + 10.0) ** 3),
    ]

    for map_a, map_b in rescales:
        f = Fuser(k=60)
        # "zeta" is rank 1 of tier_a only; "alpha" is rank 1 of tier_b
        # only; equal weights => fused("alpha") == fused("zeta")
        # bitwise. "mid" is rank 2 in both => strictly higher.
        f.add_tier(
            "tier_a", [("zeta", map_a(5.0)), ("mid", map_a(1.0))], weight=2.0
        )
        f.add_tier(
            "tier_b", [("alpha", map_b(9.0)), ("mid", map_b(3.0))], weight=2.0
        )
        top = f.top_k(3)
        scores = f.all_scores()
        assert scores["alpha"] == scores["zeta"], (
            "fixture drift: alpha/zeta were supposed to tie exactly"
        )
        assert [gid for gid, _ in top] == ["mid", "alpha", "zeta"], (
            f"fused-score tie not broken by gene_id asc under rescale; "
            f"got {top}"
        )

    # Within-tier raw-score tie: ("a", "b") tied raw => adjacent ranks
    # by gene_id asc => a strictly outranks b, before and after rescale.
    for m in (identity, lambda x: 17.0 * x + 2.0, math.exp):
        f = Fuser(k=60)
        f.add_tier(
            "t", [("b", m(2.0)), ("a", m(2.0)), ("c", m(1.0))], weight=1.0
        )
        assert _topk_order(f) == ["a", "b", "c"], (
            "within-tier raw tie must rank gene_id asc, rescale or not"
        )


# ─── 7. rank_by_score stability ───────────────────────────────────────


def test_rank_by_score_stable_under_rescale():
    """The module-level rank helper shares the (-score, gene_id) rule;
    its output is pure rank, so any monotone rescale is a no-op.
    """
    pairs = [
        ("g03", 0.91), ("g08", 0.88),
        ("g01", 0.75), ("g09", 0.75),          # tie
        ("g10", 0.60), ("g05", 0.42), ("g11", 0.30),
    ]
    base = rank_by_score(pairs)
    # Pin the exact contract once: descending score, tie by gene_id asc,
    # adjacent (not shared) ranks.
    assert base == [
        ("g03", 1), ("g08", 2), ("g01", 3), ("g09", 4),
        ("g10", 5), ("g05", 6), ("g11", 7),
    ]

    remaps = [
        [(gid, 5.5 * s - 3.0) for gid, s in pairs],       # affine
        [(gid, math.exp(s)) for gid, s in pairs],         # exp
        _dense_rank_remap(pairs),                         # arbitrary
        _spread_remap(pairs),                             # arbitrary, wild
    ]
    for i, remapped in enumerate(remaps):
        assert rank_by_score(remapped) == base, (
            f"rank_by_score changed under monotone remap #{i}"
        )
