"""Issue #430: keep the configured delivery floor through confidence tiers."""

import pytest

from cymatix_context.config import (
    AbstainClassFloors,
    BudgetConfig,
    ClassifierConfig,
    CymatixConfig,
    GenomeConfig,
    RetrievalConfig,
    RibosomeConfig,
)
from cymatix_context.context_manager import CymatixContextManager
from cymatix_context.pipeline.tier_logic import apply_budget_tiers
from tests.conftest import MockCompressorBackend, make_gene
from tests.test_abstain_tier import _stub_express


def _candidate_pool(values):
    candidates = [
        make_gene(
            f"Migration cost report {i}: verified total {100 + i} dollars.",
            gene_id=f"seat_gene_{i:010d}",
        )
        for i in range(len(values))
    ]
    return candidates, dict(zip((g.gene_id for g in candidates), values))


@pytest.mark.parametrize("top,tail,tier,tokens", [
    (12.0, 2.0, "tight", 6000),
    (8.0, 3.0, "focused", 9000),
])
@pytest.mark.parametrize("floor,expected_count", [(8, 8), (12, 12)])
def test_tier_floor_preserves_ranked_seats_and_tracks_only_cut_shadow(
    top, tail, tier, tokens, floor, expected_count,
):
    candidates, scores = _candidate_pool([top] + [tail] * 14)

    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), min_seats=floor,
    )

    assert result.budget_tier == tier
    assert result.budget_tokens_est == tokens
    assert result.candidates == candidates[:expected_count]
    assert result.shadow_pool == candidates[expected_count:]
    assert result.shadow_scores == {
        g.gene_id: tail * 0.5 for g in candidates[expected_count:]
    }


@pytest.mark.parametrize("values,tier", [
    ([24.0] + [4.0] * 4, "tight"),
    ([8.0] + [3.0] * 7, "focused"),
    ([4.0] + [3.0] * 14, "broad"),
    ([12.0, 2.0], "broad"),
    ([], "broad"),
])
def test_tier_floor_keeps_available_candidates_without_padding(values, tier):
    candidates, scores = _candidate_pool(values)

    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), min_seats=12,
    )

    assert result.budget_tier == tier
    assert result.candidates == candidates
    assert result.shadow_pool == []


@pytest.mark.parametrize("values", [
    [12.0] + [2.0] * 14,
    [8.0] + [3.0] * 14,
    [4.0] + [3.0] * 14,
    [1.0] * 15,
])
@pytest.mark.parametrize("floor", [0, 2])
def test_floor_below_tier_size_preserves_legacy_result(values, floor):
    candidates, scores = _candidate_pool(values)

    legacy = apply_budget_tiers(candidates, scores, AbstainClassFloors())
    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), min_seats=floor,
    )

    assert result == legacy


def test_tier_floor_does_not_restore_candidates_rejected_by_score_gate():
    candidates, scores = _candidate_pool([12.0] + [2.0] * 6 + [0.1] * 8)

    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), min_seats=12,
    )

    assert result.budget_tier == "tight"
    assert result.candidates == candidates[:7]
    assert result.shadow_pool == candidates[7:]


@pytest.mark.parametrize("eligible_count", [1, 2])
@pytest.mark.parametrize("tight_ratio,tier,legacy_count", [
    (3.0, "tight", 3),
    (100.0, "focused", 6),
])
@pytest.mark.parametrize("floor", [8, 12])
def test_tier_floor_does_not_expand_post_gate_sparse_fallback(
    eligible_count, tight_ratio, tier, legacy_count, floor,
):
    # Fewer than three pass the 15% gate, so the inherited fallback keeps
    # the original pool. A requested floor must not widen its weak tail.
    values = [12.0] + [2.0] * (eligible_count - 1) + [0.1] * (15 - eligible_count)
    candidates, scores = _candidate_pool(values)
    legacy = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(), tight_ratio=tight_ratio,
    )

    result = apply_budget_tiers(
        candidates, scores, AbstainClassFloors(),
        tight_ratio=tight_ratio, min_seats=floor,
    )

    assert result == legacy
    assert result.budget_tier == tier
    assert len(result.candidates) == legacy_count
    assert sum(scores[g.gene_id] < 1.8 for g in result.candidates) == legacy_count - eligible_count


@pytest.mark.parametrize("top,tail,tier,legacy_count", [
    (12.0, 2.0, "tight", 3),
    (8.0, 3.0, "focused", 6),
])
@pytest.mark.parametrize("enabled,floor", [(None, 12), (False, 12), (True, 12), (True, 0)])
def test_context_delivery_applies_tier_floor_only_when_enabled(
    top, tail, tier, legacy_count, enabled, floor,
):
    cfg = CymatixConfig(
        budget=BudgetConfig(
            max_genes_per_turn=16,
            min_delivered_docs=floor,
            expression_tokens=20000,
        ),
        classifier=ClassifierConfig(enabled=False),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        retrieval=RetrievalConfig(fusion_mode="additive"),
        ribosome=RibosomeConfig(model="mock", timeout=5),
    )
    if enabled is not None:
        cfg.budget.tier_seat_floor_enabled = enabled
    candidates, scores = _candidate_pool([top] + [tail] * 14)
    mgr = CymatixContextManager(cfg)
    mgr.ribosome.backend = MockCompressorBackend()
    try:
        for gene in candidates:
            mgr.genome.upsert_gene(gene)
        _stub_express(mgr, candidates=candidates, scores=scores)

        window = mgr.build_context("Calculate the total cost of migration.")

        expected_count = 12 if enabled and floor else legacy_count
        assert window.metadata["budget_tier"] == tier
        assert window.expressed_gene_ids == [
            g.gene_id for g in candidates[:expected_count]
        ]
    finally:
        mgr.close()
