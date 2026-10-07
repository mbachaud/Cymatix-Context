"""Cross-corpus know/miss calibration report (issue #482 tuning campaign).

The report refits the ``[know]`` logistic from ``know_abstain_replay.py``
receipts of many corpora, scores it leave-one-corpus-out, and evaluates the
shipped ``[know]`` calibration on the same rows. It never writes cymatix.toml.
"""

import importlib.util
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "know_fit_report", ROOT / "benchmarks" / "dogfood" / "know" / "know_fit_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


kfr = _load()


def _row(rank, top=1.0, gap=0.1, coord=0.5, fresh=None, needle="n"):
    return {"needle": needle, "rank_of_first_gold": rank, "top_score": top, "score_gap": gap,
            "lexical_dense_agree": False, "coordinate_confidence": coord, "freshness_min": fresh}


def test_label_is_top1_gold_and_unscored_rows_are_dropped():
    rows = [_row(1), _row(2), _row(None), {"needle": "err", "top_score": None, "rank_of_first_gold": 1}]
    out = kfr.replay_rows_to_calibration(rows, corpus="c")
    assert [r["label"] for r in out] == [1, 0, 0]
    assert all(r["corpus"] == "c" for r in out)


def test_ece_is_zero_when_probabilities_match_frequencies():
    probs = [0.25] * 4 + [0.75] * 4
    labels = [1, 0, 0, 0, 1, 1, 1, 0]
    assert kfr.expected_calibration_error(probs, labels, n_bins=10) == pytest.approx(0.0)


def test_ece_of_a_confidently_wrong_model():
    assert kfr.expected_calibration_error([0.9, 0.9], [0, 0], n_bins=10) == pytest.approx(0.9)


def test_probabilities_follow_the_shipped_formula():
    cal = {"betas": [0.5, 1.0, -1.0, 0.0, 2.0, 0.0], "s_ref": 2.0, "g_ref": 0.5}
    r = _row(1, top=2.0, gap=0.5, coord=0.25)
    z = 0.5 + 1.0 * math.tanh(1.0) - 1.0 * math.tanh(1.0) + 2.0 * 0.25
    assert kfr.probabilities(cal, [r])[0] == pytest.approx(1 / (1 + math.exp(-z)))


def test_leave_one_corpus_out_holds_each_corpus_out_once():
    rows = []
    for corpus in ("a", "b", "c"):
        rows += [dict(_row(1, top=3.0, gap=1.0, coord=0.9, needle=f"{corpus}{i}"), corpus=corpus, label=1)
                 for i in range(20)]
        rows += [dict(_row(9, top=0.1, gap=0.01, coord=0.0, needle=f"{corpus}x{i}"), corpus=corpus, label=0)
                 for i in range(20)]
    folds = kfr.leave_one_corpus_out(rows)
    assert [f["held_out"] for f in folds] == ["a", "b", "c"]
    for f in folds:
        assert f["n_train"] == 80 and f["n_test"] == 40
        assert f["auc"] == pytest.approx(1.0)


def test_report_never_touches_the_config(tmp_path, monkeypatch):
    toml = tmp_path / "cymatix.toml"
    toml.write_text("[know]\nemit_floor = 0.45\n", encoding="utf-8")
    before = toml.read_bytes()
    rows = [dict(_row(1, top=3.0, gap=1.0, coord=0.9, needle=f"p{i}"), corpus="a", label=1) for i in range(10)]
    rows += [dict(_row(9, top=0.1, gap=0.01, coord=0.0, needle=f"q{i}"), corpus="b", label=0) for i in range(10)]
    shipped = {"betas": [0.0] * 6, "s_ref": 1.0, "g_ref": 1.0, "emit_floor": 0.45}
    rep = kfr.build_report(rows, shipped)
    assert toml.read_bytes() == before
    assert set(rep) >= {"n_rows", "corpora", "shipped", "refit_all", "leave_one_corpus_out"}
    assert rep["shipped"]["emit_rate"] == 1.0  # sigmoid(0) = 0.5 >= 0.45
