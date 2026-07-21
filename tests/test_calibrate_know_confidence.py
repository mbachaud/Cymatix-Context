"""Issue #239: hit/miss AUC trust gate for the KnowBlock calibration script.

Spec: docs/specs/2026-05-08-stage-6-know-miss-blocks.md; issue #239
(``know-confidence is anti-signal on every bed — recalibrate + add hit/miss
AUC gate before agents trust the know contract``).

These tests exercise the gate at the unit level — synthetic score/label
sets and synthetic ``KnowCalibration`` bundles — so they run instantly
and never depend on a real logistic fit converging a particular way.
The ``--smoke`` end-to-end wiring (script produces betas + writes TOML
via a real, separable synthetic fit) is already covered by the script's
own ``--smoke`` self-check; this file adds the AUC-specific regression
coverage the issue calls for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cymatix_context.scoring.know_calibration import KnowCalibration
from scripts.calibrate_know_confidence import (
    DEFAULT_AUC_FLOOR,
    AUCGateError,
    compute_auc,
    gate_auc_or_raise,
    write_calibration_gated,
)


# ─── compute_auc: rank-based Mann-Whitney U formula ──────────────────────


def test_auc_perfect_separation_is_one():
    """All hits score strictly above all misses -> AUC == 1.0."""
    scores = [0.9, 0.8, 0.7, 0.2, 0.1, 0.05]
    labels = [1, 1, 1, 0, 0, 0]
    assert compute_auc(scores, labels) == pytest.approx(1.0)


def test_auc_perfect_anti_separation_is_zero():
    """All hits score strictly below all misses -> AUC == 0.0.

    This is the shape of the issue #239 bug report: misses scoring
    higher than hits on every bed (AUC 0.35-0.44, i.e. below chance).
    """
    scores = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
    labels = [1, 1, 1, 0, 0, 0]
    assert compute_auc(scores, labels) == pytest.approx(0.0)


def test_auc_no_signal_is_one_half():
    """Identical score distributions for both classes -> AUC == 0.5.

    Every hit ties with every miss, so the mid-rank tie-break must
    produce exactly chance-level AUC, not a biased value.
    """
    scores = [0.5, 0.5, 0.5, 0.5]
    labels = [1, 0, 1, 0]
    assert compute_auc(scores, labels) == pytest.approx(0.5)


def test_auc_matches_hand_computed_mann_whitney_example():
    """A small mixed example checked by hand against the U-statistic.

    hits = [0.9, 0.4], misses = [0.6, 0.3]. Pairs where hit > miss:
    (0.9>0.6) yes, (0.9>0.3) yes, (0.4>0.6) no, (0.4>0.3) yes -> 3/4 = 0.75.
    """
    scores = [0.9, 0.6, 0.4, 0.3]
    labels = [1, 0, 1, 0]
    assert compute_auc(scores, labels) == pytest.approx(0.75)


def test_auc_undefined_when_single_class():
    """No positives (or no negatives) present -> AUC is undefined (None),
    not 0.5. Callers must not silently treat this as a passing score.
    """
    assert compute_auc([0.1, 0.2, 0.3], [0, 0, 0]) is None
    assert compute_auc([0.1, 0.2, 0.3], [1, 1, 1]) is None


def test_auc_raises_on_length_mismatch():
    with pytest.raises(ValueError):
        compute_auc([0.1, 0.2], [1, 0, 0])


# ─── gate_auc_or_raise: refuse/accept decision ───────────────────────────


def test_gate_accepts_auc_above_floor():
    """AUC clears the default 0.7 floor -> no exception."""
    gate_auc_or_raise(0.85, floor=DEFAULT_AUC_FLOOR, force=False)  # must not raise


def test_gate_accepts_auc_exactly_at_floor():
    """Boundary is inclusive (>=), matching the docstring's '>= floor'."""
    gate_auc_or_raise(0.7, floor=0.7, force=False)  # must not raise


def test_gate_refuses_auc_below_floor():
    with pytest.raises(AUCGateError, match="AUC gate FAILED"):
        gate_auc_or_raise(0.44, floor=DEFAULT_AUC_FLOOR, force=False)


def test_gate_refuses_inverted_auc_below_half():
    """The literal issue #239 scenario: AUC well below chance."""
    with pytest.raises(AUCGateError):
        gate_auc_or_raise(0.352, floor=DEFAULT_AUC_FLOOR, force=False)


def test_gate_refuses_undefined_auc():
    """AUC=None (single-class holdout) must refuse, not silently pass."""
    with pytest.raises(AUCGateError, match="undefined"):
        gate_auc_or_raise(None, floor=DEFAULT_AUC_FLOOR, force=False)


def test_gate_force_overrides_low_auc_with_warning(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="helix.calibrate_know_confidence"):
        gate_auc_or_raise(0.35, floor=DEFAULT_AUC_FLOOR, force=True)  # must not raise
    assert any("OVERRIDDEN" in rec.message for rec in caplog.records)


def test_gate_force_overrides_undefined_auc_with_warning(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="helix.calibrate_know_confidence"):
        gate_auc_or_raise(None, floor=DEFAULT_AUC_FLOOR, force=True)  # must not raise
    assert any("OVERRIDDEN" in rec.message for rec in caplog.records)


def test_gate_respects_custom_floor():
    """A caller-supplied --auc-floor is honored, not the hardcoded default."""
    gate_auc_or_raise(0.6, floor=0.5, force=False)  # passes a looser floor
    with pytest.raises(AUCGateError):
        gate_auc_or_raise(0.6, floor=0.65, force=False)  # fails a tighter one


# ─── write_calibration_gated: "feed the writer" regression coverage ─────


def _fake_cal(n: int = 100) -> KnowCalibration:
    return KnowCalibration(
        betas=(-1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
        s_ref=1.0,
        g_ref=0.5,
        emit_floor=0.55,
        calibrated_at="2026-07-10T00:00:00Z",
        calibrated_on_n=n,
    )


def test_write_calibration_gated_refuses_below_floor_and_does_not_write(tmp_path):
    """Synthetic low-AUC score set -> refuse; helix.toml must be untouched."""
    out_path = tmp_path / "helix.toml"
    hit_scores = [0.2, 0.3, 0.25, 0.15]
    miss_scores = [0.6, 0.7, 0.65, 0.55]
    scores = hit_scores + miss_scores
    labels = [1, 1, 1, 1, 0, 0, 0, 0]
    auc = compute_auc(scores, labels)
    assert auc is not None and auc < DEFAULT_AUC_FLOOR

    with pytest.raises(AUCGateError):
        write_calibration_gated(out_path, _fake_cal(), auc=auc)
    assert not out_path.exists()


def test_write_calibration_gated_accepts_above_floor_and_writes(tmp_path):
    """Synthetic high-AUC score set -> accept; TOML is written with betas."""
    out_path = tmp_path / "helix.toml"
    hit_scores = [0.9, 0.85, 0.95, 0.99]
    miss_scores = [0.1, 0.2, 0.05, 0.15]
    scores = hit_scores + miss_scores
    labels = [1, 1, 1, 1, 0, 0, 0, 0]
    auc = compute_auc(scores, labels)
    assert auc is not None and auc >= DEFAULT_AUC_FLOOR

    write_calibration_gated(out_path, _fake_cal(n=250), auc=auc)
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "[know]" in text
    assert "betas" in text
    assert "calibrated_on_n = 250" in text


def test_write_calibration_gated_force_writes_despite_low_auc(tmp_path):
    """--force bypasses a failing gate and still writes the file."""
    out_path = tmp_path / "helix.toml"
    scores = [0.2, 0.3, 0.6, 0.7]  # inverted: hits below misses
    labels = [1, 1, 0, 0]
    auc = compute_auc(scores, labels)
    assert auc is not None and auc < DEFAULT_AUC_FLOOR

    write_calibration_gated(out_path, _fake_cal(), auc=auc, force=True)
    assert out_path.exists()
    assert "[know]" in out_path.read_text(encoding="utf-8")


def test_write_calibration_gated_updates_existing_toml_only_above_floor(tmp_path):
    """Below-floor gate must not clobber a pre-existing helix.toml either."""
    out_path = tmp_path / "helix.toml"
    out_path.write_text(
        "[server]\nport = 11437\n\n[know]\nemit_floor = 0.55\nbetas = [0.0]\n",
        encoding="utf-8",
    )
    original = out_path.read_text(encoding="utf-8")

    scores = [0.1, 0.2, 0.8, 0.9]
    labels = [1, 1, 0, 0]  # inverted -> AUC 0.0
    auc = compute_auc(scores, labels)

    with pytest.raises(AUCGateError):
        write_calibration_gated(out_path, _fake_cal(), auc=auc)
    assert out_path.read_text(encoding="utf-8") == original


# ─── CLI main(): non-zero exit code + no file write on refuse ───────────


def test_main_returns_nonzero_and_prints_error_on_gate_failure(tmp_path, capsys):
    """End-to-end CLI: an --input bench with anti-correlated features/labels
    across the WHOLE held-out set must exit non-zero and leave --out
    untouched.

    Uses a bench where ``label`` is assigned by a rule the feature columns
    cannot linearly recover (parity of an unrelated counter), so a
    logistic fit trained on it has no real signal and the held-out AUC
    lands near chance -- comfortably under the 0.7 floor with the fixed
    seed used here.
    """
    import json as _json
    from scripts.calibrate_know_confidence import main as cli_main

    input_path = tmp_path / "bench.jsonl"
    out_path = tmp_path / "helix.toml"
    rows = []
    for i in range(60):
        rows.append({
            "top_score": 0.5,
            "score_gap": 0.1,
            "lexical_dense_agree": (i % 2 == 0),
            "coordinate_confidence": 0.5,
            "freshness_min": 0.5,
            # Label independent of every feature column above -> no
            # linear signal for the fitter to recover.
            "label": (i % 7 == 0),
        })
    input_path.write_text(
        "\n".join(_json.dumps(r) for r in rows), encoding="utf-8",
    )

    rc = cli_main([
        "--input", str(input_path),
        "--out", str(out_path),
        "--seed", "42",
    ])
    assert rc != 0
    assert not out_path.exists()
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
