"""Tests for the #239 eval harness (benchmarks/eval_retrieval.py) and
the located_n1000 calibration-data generator's feature extraction.

The metric primitives are pure functions — pinned against hand-computed
values. The generator's feature extraction is exercised against a
stubbed manager so the row schema stays locked to what
scripts/calibrate_know_confidence._row_to_features consumes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO / "benchmarks"), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval_retrieval import (  # noqa: E402
    auc_mann_whitney,
    build_report,
    ece,
    load_calibration,
    mrr,
    retrieval_at,
    risk_coverage,
)


# ── AUC ──────────────────────────────────────────────────────────────


def test_auc_perfect_separation():
    assert auc_mann_whitney([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0


def test_auc_inverted():
    assert auc_mann_whitney([0.1, 0.2, 0.8, 0.9], [1, 1, 0, 0]) == 0.0


def test_auc_random_ties():
    # All scores tied → AUC 0.5 by tie correction.
    assert auc_mann_whitney([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]) == pytest.approx(0.5)


def test_auc_degenerate_single_class():
    assert auc_mann_whitney([0.9, 0.8], [1, 1]) == 0.5


# ── ECE ──────────────────────────────────────────────────────────────


def test_ece_perfectly_calibrated_extremes():
    # conf 1.0 always right, conf 0.0 always wrong → ECE 0.
    assert ece([1.0, 1.0, 0.0, 0.0], [1, 1, 0, 0]) == pytest.approx(0.0)


def test_ece_maximally_miscalibrated():
    # confident and always wrong → ECE 1.0
    assert ece([1.0, 1.0], [0, 0]) == pytest.approx(1.0)


def test_ece_hand_computed_two_bins():
    # bin [0.6,0.7): two rows conf 0.6, acc 0.5 → |0.5-0.6| = 0.1, weight 0.5
    # bin [0.2,0.3): two rows conf 0.2, acc 0.0 → |0.0-0.2| = 0.2, weight 0.5
    got = ece([0.6, 0.6, 0.2, 0.2], [1, 0, 0, 0], n_bins=10)
    assert got == pytest.approx(0.5 * 0.1 + 0.5 * 0.2)


# ── risk-coverage ────────────────────────────────────────────────────


def test_risk_coverage_ordering_and_risk():
    confs = [0.9, 0.8, 0.3, 0.1]
    labels = [1, 1, 0, 0]
    rc = risk_coverage(confs, labels, points=(0.5, 1.0))
    # top-50% coverage covers the two confident correct rows → risk 0
    assert rc[0]["coverage"] == 0.5 and rc[0]["risk"] == 0.0
    # full coverage → risk = 2 errors / 4
    assert rc[1]["coverage"] == 1.0 and rc[1]["risk"] == pytest.approx(0.5)


# ── rank metrics ─────────────────────────────────────────────────────


def test_mrr_and_retrieval_at():
    ranks = [1, 2, -1, 5]
    assert mrr(ranks) == pytest.approx((1.0 + 0.5 + 0.0 + 0.2) / 4)
    assert retrieval_at(ranks, 1) == pytest.approx(0.25)
    assert retrieval_at(ranks, 5) == pytest.approx(0.75)


# ── calibration loading + report shape ───────────────────────────────


def test_load_calibration_default_matches_module_defaults():
    from cymatix_context.scoring.know_calibration import (
        DEFAULT_BETAS,
        DEFAULT_EMIT_FLOOR,
    )

    cal = load_calibration("default")
    assert tuple(cal.betas) == tuple(DEFAULT_BETAS)
    assert cal.emit_floor == DEFAULT_EMIT_FLOOR


def test_load_calibration_from_toml(tmp_path):
    p = tmp_path / "know.toml"
    p.write_text(
        "[know]\nemit_floor = 0.7\ns_ref = 2.0\ng_ref = 1.0\n"
        "betas = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]\n"
    )
    cal = load_calibration(str(p))
    assert cal.emit_floor == 0.7
    assert cal.s_ref == 2.0
    assert list(cal.betas) == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]


def test_build_report_shape():
    rows = [
        {
            "label": 1,
            "planted_rank": 1,
            "expressed_rank": 1,
            "top_score": 8.0,
            "score_gap": 3.0,
            "lexical_dense_agree": True,
            "coordinate_confidence": 0.8,
            "freshness_min": 1.0,
        },
        {
            "label": 0,
            "planted_rank": -1,
            "expressed_rank": -1,
            "top_score": 0.5,
            "score_gap": 0.1,
            "lexical_dense_agree": False,
            "coordinate_confidence": 0.0,
            "freshness_min": None,
        },
    ]
    rep = build_report(rows, load_calibration("default"), "default")
    assert rep["n"] == 2
    assert rep["retrieval_at_1"] == pytest.approx(0.5)
    assert rep["mrr"] == pytest.approx(0.5)
    assert 0.0 <= rep["auc"] <= 1.0
    assert 0.0 <= rep["ece_10bin"] <= 1.0
    assert len(rep["risk_coverage"]) == 10
    assert rep["expressed_rate"] == pytest.approx(0.5)


# ── located_n1000 feature extraction against a stubbed manager ───────


def _stub_manager(scores: dict, expressed_ids: list):
    from cymatix_context.config import (
        BudgetConfig,
        ClassifierConfig,
        GenomeConfig,
        HelixConfig,
        RibosomeConfig,
    )
    from cymatix_context.context_manager import HelixContextManager
    from tests.conftest import make_gene

    cfg = HelixConfig(
        ribosome=RibosomeConfig(model="mock", timeout=5),
        budget=BudgetConfig(max_genes_per_turn=12, abstain_enabled=False),
        genome=GenomeConfig(path=":memory:", cold_start_threshold=5),
        classifier=ClassifierConfig(enabled=False),
    )
    mgr = HelixContextManager(cfg)
    candidates = [make_gene(f"content {gid}", gene_id=gid) for gid in expressed_ids]

    def fake_express(domains, entities, max_genes, **_kwargs):
        mgr.genome.last_query_scores = dict(scores)
        return list(candidates)

    mgr._retrieve = fake_express
    mgr._express = fake_express

    def fake_refiners(query, cands, max_genes, **_kwargs):
        return list(cands), {}

    mgr._apply_candidate_refiners = fake_refiners
    return mgr


def test_features_for_query_row_contract():
    from located_n1000 import features_for_query

    scores = {"g_top": 5.0, "g_second": 3.0, "g_third": 1.0}
    mgr = _stub_manager(scores, ["g_top", "g_second", "g_third"])
    try:
        feats = features_for_query(mgr, "what is the frobnicator limit")
    finally:
        mgr.close()

    assert feats["retrieved_top1"] == "g_top"
    assert feats["top_score"] == pytest.approx(5.0)
    assert feats["score_gap"] == pytest.approx(2.0)
    assert isinstance(feats["lexical_dense_agree"], bool)
    assert 0.0 <= feats["coordinate_confidence"] <= 1.0
    assert feats["ranked_ids"][0] == "g_top"
    # the exact keys calibrate_know_confidence._row_to_features reads
    for key in (
        "top_score",
        "score_gap",
        "lexical_dense_agree",
        "coordinate_confidence",
        "freshness_min",
    ):
        assert key in feats
