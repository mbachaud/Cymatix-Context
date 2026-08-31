"""W2.2-narrow: eps_band_coverage combinator (distinct-term coverage tie-break).

Evidence basis: benchmarks/dogfood/erb/receipts/semantic_above_gold_947k_2026-08-31.json
— on the 21 rank-reachable ERB semantic misses, gold out-covers the interloper
majority on distinct query terms in 16/21. The combinator breaks fused
near-ties inside the ε band by coverage first, then rerank, then fused, then
gene_id. Ships default-inert: nothing selects it unless configured.
"""
from __future__ import annotations

import pytest

from cymatix_context.retrieval.rerank_combinators import (
    VALID_COMBINATORS,
    combine_rerank,
)


FUSED = {
    # band 1 (leader a, delta 0.10 -> threshold 0.90): a, b, c
    "a": 1.00, "b": 0.95, "c": 0.92,
    # band 2 (leader d, threshold 0.45): d, e
    "d": 0.50, "e": 0.47,
    # zero tail
    "f": 0.0,
}


def test_valid_combinators_includes_eps_band_coverage():
    assert "eps_band_coverage" in VALID_COMBINATORS


def test_in_band_reorder_by_coverage():
    coverage = {"c": 9.0, "b": 5.0, "a": 1.0, "e": 3.0, "d": 1.0}
    final, ranked = combine_rerank(
        "eps_band_coverage", FUSED, {}, {}, k=20, tier_weight=0.2,
        delta=0.10, limit=0, coverage=coverage,
    )
    # band 1 reorders c > b > a; band 2 reorders e > d; zero tail untouched.
    assert ranked == ["c", "b", "a", "e", "d", "f"]
    # final scores stay pure fused (eps semantics preserved).
    assert final == FUSED


def test_no_cross_band_movement():
    # e out-covers everything, but sits in band 2 — it must not cross into
    # band 1 no matter how large its coverage is.
    coverage = {"e": 99.0}
    _, ranked = combine_rerank(
        "eps_band_coverage", FUSED, {}, {}, k=20, tier_weight=0.2,
        delta=0.10, limit=0, coverage=coverage,
    )
    assert ranked.index("e") >= 3
    assert ranked[:3] == ["a", "b", "c"]  # equal coverage inside band 1 -> fused order


def test_coverage_ties_fall_back_to_rerank_then_fused():
    coverage = {"a": 2.0, "b": 2.0, "c": 2.0}
    rerank = {"b": 0.5}
    _, ranked = combine_rerank(
        "eps_band_coverage", FUSED, rerank, {}, k=20, tier_weight=0.2,
        delta=0.10, limit=0, coverage=coverage,
    )
    # coverage tied at 2.0 across band 1 -> rerank lifts b; a/c stay fused order.
    assert ranked[:3] == ["b", "a", "c"]


def test_empty_coverage_matches_eps_band_exactly():
    rerank = {"c": 0.4, "e": 0.1}
    base_final, base_ranked = combine_rerank(
        "eps_band", FUSED, rerank, {}, k=20, tier_weight=0.2,
        delta=0.10, limit=0,
    )
    cov_final, cov_ranked = combine_rerank(
        "eps_band_coverage", FUSED, rerank, {}, k=20, tier_weight=0.2,
        delta=0.10, limit=0, coverage=None,
    )
    assert cov_ranked == base_ranked
    assert cov_final == base_final


def test_limit_truncates():
    _, ranked = combine_rerank(
        "eps_band_coverage", FUSED, {}, {}, k=20, tier_weight=0.2,
        delta=0.10, limit=2, coverage={"b": 3.0},
    )
    assert ranked == ["b", "a"]


def test_config_map_accepts_eps_band_coverage(tmp_path):
    from cymatix_context.config import load_config

    toml = tmp_path / "cymatix.toml"
    toml.write_text(
        "[retrieval]\n"
        'rerank_combinator_by_class = { default = "eps_band_coverage" }\n'
    )
    cfg = load_config(str(toml))
    assert cfg.retrieval.rerank_combinator_by_class["default"] == "eps_band_coverage"


def test_other_combinators_reject_coverage_silently():
    # coverage is ignored by non-coverage combinators (no behavior change).
    fin_a, rank_a = combine_rerank(
        "additive", FUSED, {}, {}, k=20, tier_weight=0.2, delta=0.10,
        limit=0, coverage={"f": 50.0},
    )
    fin_b, rank_b = combine_rerank(
        "additive", FUSED, {}, {}, k=20, tier_weight=0.2, delta=0.10, limit=0,
    )
    assert (fin_a, rank_a) == (fin_b, rank_b)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
