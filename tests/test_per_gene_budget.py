"""Tests for the uniform-path per-gene char-budget allocator.

The allocator decides target_chars for each candidate gene in the
non-foveated splice loop of build_context. Two modes:

  "fixed"   — every gene gets ``fixed_target`` (byte-identical to the legacy
              hard-coded ``target = 1000``).
  "dynamic" — floor-then-greedy: every gene keeps at least ``floor_chars``
              (so no gene regresses vs fixed), then surplus budget is handed
              to genes in rank order, each raised up to
              ``min(gene_length, ceiling_chars)`` until the budget runs out.

compress_text returns content verbatim when ``len(content) <= target_chars``,
so a target >= a gene's length means that gene is delivered whole.
"""
from __future__ import annotations

from helix_context.pipeline.per_gene_budget import compute_uniform_targets


def test_empty_returns_empty():
    assert compute_uniform_targets([], mode="dynamic") == []


def test_fixed_mode_is_legacy_flat_target():
    # Byte-identical to the old `target = 1000` for every candidate.
    assert compute_uniform_targets([5000, 3000, 800], mode="fixed",
                                   fixed_target=1000) == [1000, 1000, 1000]


def test_dynamic_single_gene_breathes_to_full_length():
    # A 5000-char gene with ample budget and a 12000 ceiling should get its
    # full length as target -> compress_text delivers it whole.
    out = compute_uniform_targets([5000], mode="dynamic",
                                  total_budget_chars=28000,
                                  ceiling_chars=12000, floor_chars=1000)
    assert out == [5000]


def test_dynamic_caps_at_ceiling():
    # A 20000-char whale must be capped at the per-gene ceiling so it can't
    # eat the whole prompt.
    out = compute_uniform_targets([20000], mode="dynamic",
                                  total_budget_chars=28000,
                                  ceiling_chars=12000, floor_chars=1000)
    assert out == [12000]


def test_dynamic_gene_shorter_than_floor():
    # A gene shorter than the floor only needs its own length (no point
    # targeting more than exists).
    out = compute_uniform_targets([500], mode="dynamic",
                                  total_budget_chars=28000,
                                  ceiling_chars=12000, floor_chars=1000)
    assert out == [500]


def test_dynamic_never_below_fixed_floor_per_gene():
    # With many candidates, every gene still gets at least floor_chars
    # (no dropped/starved gene vs the legacy 1000 behavior).
    lengths = [9000, 9000, 9000, 9000, 9000]   # 5 genes, each big
    out = compute_uniform_targets(lengths, mode="dynamic",
                                  total_budget_chars=28000,
                                  ceiling_chars=12000, floor_chars=1000)
    assert all(t >= 1000 for t in out), out
    assert len(out) == 5


def test_dynamic_total_within_budget_and_greedy_top_first():
    # Budget is the binding constraint: total allocated must not exceed it,
    # and the top-ranked gene should be filled before later ones.
    lengths = [9000, 9000, 9000, 9000, 9000]
    budget = 28000
    out = compute_uniform_targets(lengths, mode="dynamic",
                                  total_budget_chars=budget,
                                  ceiling_chars=12000, floor_chars=1000)
    assert sum(out) <= budget
    # rank order: earlier (higher-ranked) genes get >= later ones
    assert out == sorted(out, reverse=True), out
    # the top gene should have breathed beyond the floor
    assert out[0] > 1000


def test_dynamic_surplus_fully_used_when_genes_can_absorb_it():
    # Two 5000-char genes, 28000 budget, 12000 ceiling: both fit whole.
    out = compute_uniform_targets([5000, 5000], mode="dynamic",
                                  total_budget_chars=28000,
                                  ceiling_chars=12000, floor_chars=1000)
    assert out == [5000, 5000]


# ── Relevance-aware surplus (H10p, content-aware allocator) ─────────────
#
# When the caller passes ``relevance_scores`` parallel to ``content_lengths``,
# the dynamic-mode surplus is granted in DESCENDING-relevance order instead
# of input (rank) order. Use case: the retrieval ranker placed the gold gene
# at low rank, but a lexical/BM25 signal says it has the highest query overlap
# in its content -> boost its target so the answer fact in its body isn't
# truncated. The result by H10n is that uniform-floor lifts don't close the
# depth-correctness gap because they redistribute against the dominant
# gold-at-top case; this knob is the targeted alternative.

def test_dynamic_relevance_scores_reorder_surplus():
    # Equal lengths, equal floor, relevance flipped vs input order:
    # surplus must go to the highest-relevance gene first, not the first one.
    lengths = [10000, 10000, 10000]
    out_no_rel = compute_uniform_targets(
        lengths, mode="dynamic", total_budget_chars=20000,
        ceiling_chars=12000, floor_chars=1000,
    )
    out_rel = compute_uniform_targets(
        lengths, mode="dynamic", total_budget_chars=20000,
        ceiling_chars=12000, floor_chars=1000,
        relevance_scores=[0.1, 0.5, 0.9],
    )
    # default: surplus to gene 0 first
    assert out_no_rel[0] > out_no_rel[2], out_no_rel
    # relevance-aware: surplus to gene 2 first
    assert out_rel[2] > out_rel[0], out_rel
    assert out_rel[2] >= out_rel[1] >= out_rel[0]
    assert sum(out_rel) <= 20000


def test_dynamic_relevance_scores_none_is_byte_identical_default():
    # The default (relevance_scores=None) MUST reproduce the prior behavior
    # exactly, so the new parameter is opt-in.
    lengths = [9000, 9000, 9000, 9000, 9000]
    a = compute_uniform_targets(
        lengths, mode="dynamic", total_budget_chars=28000,
        ceiling_chars=12000, floor_chars=1000,
    )
    b = compute_uniform_targets(
        lengths, mode="dynamic", total_budget_chars=28000,
        ceiling_chars=12000, floor_chars=1000,
        relevance_scores=None,
    )
    assert a == b


def test_dynamic_relevance_scores_ignored_in_fixed_mode():
    # Fixed mode is the legacy byte-identical path -- relevance signal has
    # no effect there.
    lengths = [9000, 9000, 9000]
    out = compute_uniform_targets(
        lengths, mode="fixed", fixed_target=1000,
        relevance_scores=[0.9, 0.5, 0.1],
    )
    assert out == [1000, 1000, 1000]


def test_dynamic_relevance_ties_preserve_input_order():
    # If two genes have equal relevance, the tiebreaker is input (rank)
    # order -- so a non-informative all-equal signal degenerates to the
    # current rank-order behavior.
    lengths = [10000, 10000, 10000]
    out = compute_uniform_targets(
        lengths, mode="dynamic", total_budget_chars=20000,
        ceiling_chars=12000, floor_chars=1000,
        relevance_scores=[0.5, 0.5, 0.5],
    )
    # All equal -> input-order surplus -> gene 0 wins
    assert out[0] >= out[1] >= out[2]


def test_dynamic_relevance_length_mismatch_raises():
    # Defensive: a mismatched relevance_scores length is a caller bug,
    # not silent fallback.
    import pytest
    with pytest.raises(ValueError):
        compute_uniform_targets(
            [10000, 10000], mode="dynamic", total_budget_chars=10000,
            ceiling_chars=12000, floor_chars=1000,
            relevance_scores=[0.5, 0.5, 0.5],   # length 3 vs 2 lengths
        )
