"""#239 B1 — know-calibration monotonicity guard.

Every know-feature is oriented so a *larger* value means *stronger*
retrieval evidence (higher top_score, wider score_gap, tiers agree,
coordinate-aligned, fresher). The confidence logistic must therefore be
non-decreasing in each feature, i.e. every feature coefficient b1..b5
must be >= 0.

The 2026-07-06 fit (#249) shipped b1 = -1.1442 — a negative coefficient
on top_score, so *better* retrieval *lowered* confidence. That inversion
is the mechanism behind the ~0.42 confidence ceiling that sits below
emit_floor (structural zero recall). These tests pin the checker that
detects such inversions and the warning it emits, so a future re-fit
cannot silently re-ship a non-monotonic vector.

See docs/research/2026-07-08-b1-operating-point-coupling.md.
"""

from __future__ import annotations

import logging

from cymatix_context.scoring.know_calibration import (
    DEFAULT_BETAS,
    FEATURE_NAMES,
    KnowCalibration,
    calibration_from_config,
    monotonicity_violations,
)


def test_default_betas_are_monotonic():
    """The code-default vector is correctly oriented (all b1..b5 >= 0)."""
    assert monotonicity_violations(DEFAULT_BETAS) == []


def test_flags_negative_top_score_coefficient():
    """The shipped-style inversion (b1<0) is flagged by name."""
    betas = (-2.1222, -1.1442, 0.8794, 0.9407, 0.2999, 0.7979)
    assert monotonicity_violations(betas) == ["top_score"]


def test_flags_every_negative_feature_but_not_intercept():
    """Intercept (b0) sign is unconstrained; each negative feature is named."""
    # b0 negative (fine), b2 (score_gap) and b5 (freshness_min) negative.
    betas = (-5.0, 1.0, -0.5, 0.7, 1.8, -0.3)
    assert monotonicity_violations(betas) == ["score_gap", "freshness_min"]


def test_feature_names_align_with_five_features():
    assert len(FEATURE_NAMES) == 5
    assert FEATURE_NAMES[0] == "top_score"


def test_short_betas_tuple_does_not_raise():
    """A 5-length (freshness-less) vector only checks the features present."""
    assert monotonicity_violations((-2.0, 2.0, 1.5, 0.7, 1.8)) == []
    assert monotonicity_violations((-2.0, -2.0, 1.5, 0.7, 1.8)) == ["top_score"]


class _FakeKnowCfg:
    """Minimal stand-in for config.KnowConfig (calibration_from_config duck-types it)."""

    def __init__(self, betas):
        self.betas = list(betas)
        self.s_ref = 4.2503
        self.g_ref = 0.4386
        self.emit_floor = 0.45
        self.calibrated_at = None
        self.calibrated_on_n = None
        self.stale_after_days = 30


def test_calibration_from_config_warns_on_non_monotonic(caplog):
    cfg = _FakeKnowCfg((-2.1222, -1.1442, 0.8794, 0.9407, 0.2999, 0.7979))
    with caplog.at_level(logging.WARNING, logger="helix.know_calibration"):
        cal = calibration_from_config(cfg)
    assert isinstance(cal, KnowCalibration)
    # betas pass through unchanged (the guard warns, it does not mutate).
    assert cal.betas[1] == -1.1442
    assert any("non-monotonic" in r.message and "top_score" in r.message
               for r in caplog.records)
    assert any("239" in r.message for r in caplog.records)


def test_calibration_from_config_quiet_on_monotonic(caplog):
    cfg = _FakeKnowCfg(DEFAULT_BETAS)
    with caplog.at_level(logging.WARNING, logger="helix.know_calibration"):
        calibration_from_config(cfg)
    assert not any("non-monotonic" in r.message for r in caplog.records)
