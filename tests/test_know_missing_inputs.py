"""Know/miss inputs that are UNAVAILABLE must not be scored as evidence (#482).

With dense retrieval off (the shipped default since 2026-08-15) there is no
dense lane, so "lexical and dense agree" is not False -- it is unknown. Same
for coordinate confidence when nothing was delivered. compute_confidence
already treats freshness_min=None as unknown; this extends that to the other
inputs and adds an optional [know] ``neutral`` vector: an unknown feature
contributes beta_i * neutral_i (the feature's mean in the calibration set),
the neutral choice for a logistic. Without ``neutral`` configured, every
result is byte-identical to the previous behaviour.
"""

import math
from pathlib import Path

import pytest

from cymatix_context.config import load_config
from cymatix_context.scoring.know_calibration import (
    KnowCalibration,
    calibration_from_config,
    compute_confidence,
    know_profile_warnings,
    producible_inputs,
    profile_mismatch,
)
from cymatix_context.scoring.know_decision import (
    _agree_from_tier_contributions,
    _agree_or_unknown,
    _decide_know_or_miss_impl,
)
from cymatix_context.schemas import ContextHealth, ContextWindow, KnowBlock

BETAS = (-1.0, 1.0, 1.0, 2.0, 1.5, 0.5)


def _z(top, gap, agree_term, coord_term, fresh_term, cal):
    z = cal.betas[0] + cal.betas[1] * math.tanh(top / cal.s_ref) + cal.betas[2] * math.tanh(gap / cal.g_ref)
    z += cal.betas[3] * agree_term + cal.betas[4] * coord_term + cal.betas[5] * fresh_term
    return 1.0 / (1.0 + math.exp(-z))


def test_unknown_inputs_without_neutral_are_byte_identical_to_legacy():
    cal = KnowCalibration(betas=BETAS, s_ref=1.0, g_ref=1.0)
    legacy = compute_confidence(top_score=0.5, score_gap=0.1, lexical_dense_agree=False,
                                coordinate_confidence=0.0, calibration=cal, freshness_min=None)
    unknown = compute_confidence(top_score=0.5, score_gap=0.1, lexical_dense_agree=None,
                                 coordinate_confidence=None, calibration=cal, freshness_min=None)
    assert unknown == legacy


def test_unknown_inputs_take_the_neutral_value_when_configured():
    cal = KnowCalibration(betas=BETAS, s_ref=1.0, g_ref=1.0, neutral=(0.3, 0.2, 0.6, 0.4, 0.9))
    got = compute_confidence(top_score=0.5, score_gap=0.1, lexical_dense_agree=None,
                             coordinate_confidence=None, calibration=cal, freshness_min=None)
    assert got == pytest.approx(_z(0.5, 0.1, 0.6, 0.4, 0.9, cal))


def test_known_inputs_ignore_the_neutral_vector():
    cal = KnowCalibration(betas=BETAS, s_ref=1.0, g_ref=1.0, neutral=(0.3, 0.2, 0.6, 0.4, 0.9))
    got = compute_confidence(top_score=0.5, score_gap=0.1, lexical_dense_agree=False,
                             coordinate_confidence=0.0, calibration=cal, freshness_min=1.0)
    assert got == pytest.approx(_z(0.5, 0.1, 0.0, 0.0, 1.0, cal))


def test_agree_is_unknown_without_a_dense_lane():
    lexical_only = {"g1": {"fts5": 1.0}, "g2": {"fts5": 0.5, "tag_exact": 0.2}}
    assert _agree_from_tier_contributions(lexical_only, k=3) is False  # legacy contract kept
    assert _agree_or_unknown(lexical_only, k=3) is None
    assert _agree_or_unknown({}, k=3) is None
    assert _agree_or_unknown(None, k=3) is None


def test_agree_is_decided_when_both_lanes_fired():
    both = {"g1": {"fts5": 1.0, "dense": 0.9}, "g2": {"fts5": 0.5}, "g3": {"dense": 0.4}}
    assert _agree_or_unknown(both, k=3) is _agree_from_tier_contributions(both, k=3)


def _healthy_window():
    # Same shape as tests/test_know_miss_block.py::healthy_window.
    return ContextWindow(
        ribosome_prompt="", expressed_context="(genes here)", expressed_gene_ids=["g1"],
        context_health=ContextHealth(ellipticity=0.95, coverage=0.7, density=0.8, freshness=1.0,
                                     genes_available=100, genes_expressed=4, status="aligned"))


def test_know_block_wire_values_stay_typed_when_inputs_are_unknown():
    cal = KnowCalibration(betas=(5.0, 0.0, 0.0, 0.0, 0.0, 0.0), s_ref=1.0, g_ref=1.0, emit_floor=0.5)
    block = _decide_know_or_miss_impl(
        _healthy_window(), query="q", top_score=1.0, score_gap=0.5, lexical_dense_agree=None,
        coordinate_confidence=None, top_gene=None, ratio=2.0, calibration=cal)
    assert isinstance(block, KnowBlock)
    assert block.lexical_dense_agree is False
    assert block.coordinate_confidence == 0.0


def test_config_parses_neutral_and_rejects_the_wrong_length(tmp_path: Path, caplog):
    good = tmp_path / "good.toml"
    good.write_text("[know]\nneutral = [0.1, 0.2, 0.3, 0.4, 0.5]\n", encoding="utf-8")
    cfg = load_config(str(good))
    assert cfg.know.neutral == [0.1, 0.2, 0.3, 0.4, 0.5]
    assert calibration_from_config(cfg.know).neutral == (0.1, 0.2, 0.3, 0.4, 0.5)

    bad = tmp_path / "bad.toml"
    bad.write_text("[know]\nneutral = [0.1, 0.2]\n", encoding="utf-8")
    assert load_config(str(bad)).know.neutral is None


def test_shipped_config_has_no_neutral_vector():
    root = Path(__file__).resolve().parents[1]
    assert load_config(str(root / "cymatix.toml")).know.neutral is None


# ── Binding to the live config (user ask 2026-10-05): which inputs the
#    enabled lanes can produce vs which inputs the calibration was fit on ──

ALL = {"top_score", "score_gap", "lexical_dense_agree", "coordinate_confidence", "freshness_min"}


def test_producible_inputs_follow_the_enabled_lanes(tmp_path: Path):
    off = tmp_path / "off.toml"
    off.write_text("[retrieval]\ndense_embedding_enabled = false\n", encoding="utf-8")
    assert producible_inputs(load_config(str(off))) == ALL - {"lexical_dense_agree"}
    on = tmp_path / "on.toml"
    on.write_text("[retrieval]\ndense_embedding_enabled = true\n", encoding="utf-8")
    assert producible_inputs(load_config(str(on))) == ALL
    splade = tmp_path / "splade.toml"
    splade.write_text("[ingestion]\nsplade_enabled = true\n", encoding="utf-8")
    assert "lexical_dense_agree" in producible_inputs(load_config(str(splade)))


def test_an_input_the_calibration_was_not_fit_on_is_treated_as_unknown():
    cal = KnowCalibration(betas=BETAS, s_ref=1.0, g_ref=1.0, neutral=(0.3, 0.2, 0.6, 0.4, 0.9),
                          fitted_inputs=tuple(sorted(ALL - {"lexical_dense_agree"})))
    kw = dict(top_score=0.5, score_gap=0.1, coordinate_confidence=0.2, calibration=cal, freshness_min=1.0)
    assert compute_confidence(lexical_dense_agree=True, **kw) == compute_confidence(lexical_dense_agree=None, **kw)


def test_an_input_the_live_config_cannot_produce_is_treated_as_unknown():
    cal = KnowCalibration(betas=BETAS, s_ref=1.0, g_ref=1.0, neutral=(0.3, 0.2, 0.6, 0.4, 0.9))
    kw = dict(top_score=0.5, score_gap=0.1, coordinate_confidence=0.2, calibration=cal, freshness_min=1.0)
    masked = compute_confidence(lexical_dense_agree=True, live_inputs=ALL - {"lexical_dense_agree"}, **kw)
    assert masked == compute_confidence(lexical_dense_agree=None, **kw)
    assert compute_confidence(lexical_dense_agree=True, live_inputs=ALL, **kw) != masked


def test_profile_mismatch_names_the_inputs_that_differ():
    fitted = KnowCalibration(fitted_inputs=tuple(sorted(ALL)))
    assert profile_mismatch(fitted, ALL - {"lexical_dense_agree"}) == ["lexical_dense_agree"]
    assert profile_mismatch(fitted, ALL) == []
    assert profile_mismatch(KnowCalibration(), ALL - {"lexical_dense_agree"}) == []  # unknown fit profile


def test_config_parses_fitted_inputs_and_rejects_unknown_names(tmp_path: Path):
    good = tmp_path / "good.toml"
    good.write_text('[know]\nfitted_inputs = ["top_score", "score_gap"]\n', encoding="utf-8")
    know = load_config(str(good)).know
    assert know.fitted_inputs == ["top_score", "score_gap"]
    assert calibration_from_config(know).fitted_inputs == ("top_score", "score_gap")
    bad = tmp_path / "bad.toml"
    bad.write_text('[know]\nfitted_inputs = ["top_score", "bogus"]\n', encoding="utf-8")
    assert load_config(str(bad)).know.fitted_inputs is None


def test_profile_warning_only_when_the_fit_profile_is_known_and_differs():
    fitted = KnowCalibration(fitted_inputs=tuple(sorted(ALL)))
    assert know_profile_warnings(fitted, ALL - {"lexical_dense_agree"}) == ["calibration_profile_mismatch"]
    assert know_profile_warnings(fitted, ALL) == []
    assert know_profile_warnings(KnowCalibration(), ALL - {"lexical_dense_agree"}) == []
