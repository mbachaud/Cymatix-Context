"""Dense-free know/miss feature probe (issue #482).

The shipped [know] logistic has a lexical_dense_agree input that is always
False with dense retrieval off. The probe asks which inputs the shipped path
DOES deliver separate hits from misses on held-out corpora. Features must never
read gold-derived fields.
"""

import importlib.util
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "know_feature_probe", ROOT / "benchmarks" / "dogfood" / "know" / "know_feature_probe.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


probe = _load()

ROW = {"top_score": 0.2, "second_score": 0.1, "score_gap": 0.1, "ratio_top2": 2.0, "pool_size": 48,
       "delivered_count": 12, "coordinate_confidence": 0.5, "budget_tier": "broad",
       "classifier_class": "default", "health_status": "sparse",
       "rank_of_first_gold": 1, "delivered_gold": 1, "gold_ranks": [1], "final_rank_of_first_gold": 1}


def test_scale_free_features():
    f = probe.feature_vector(ROW, "scale_free")
    assert f == pytest.approx([math.log(2.0), 0.5, 0.5, math.log(48)])


def test_features_never_read_gold_fields():
    gold_free = {k: v for k, v in ROW.items() if k not in probe.GOLD_FIELDS}
    for name in probe.FEATURE_SETS:
        assert probe.feature_vector(ROW, name) == probe.feature_vector(gold_free, name)


def test_categoricals_are_one_hot_over_known_levels():
    f = probe.feature_vector(ROW, "all_served")
    tiers = [f[i] for i, n in enumerate(probe.feature_names("all_served")) if n.startswith("budget_tier=")]
    assert sum(tiers) == 1.0


def test_agreement_features_carry_explicit_missing_flags():
    row = dict(ROW, lane_signals={"lanes_fired": 3, "top1_lanes": 2, "lanes_top3_agree": 2,
                                  "frac_lanes_agree": 2 / 3, "fts5_top1_is_fused_top1": True,
                                  "query_term_coverage": None})
    names = probe.feature_names("agreement")
    f = dict(zip(names, probe.feature_vector(row, "agreement")))
    assert f["frac_lanes_agree"] == pytest.approx(2 / 3) and f["frac_lanes_agree_missing"] == 0.0
    assert f["fts5_top1_is_fused_top1"] == 1.0
    assert f["query_term_coverage"] == 0.0 and f["query_term_coverage_missing"] == 1.0
    no_sig = dict(zip(names, probe.feature_vector(dict(ROW), "agreement")))
    assert no_sig["lanes_fired_missing"] == 1.0


def test_labels():
    assert probe.label(ROW, "rank1") == 1
    assert probe.label(dict(ROW, rank_of_first_gold=3), "rank1") == 0
    assert probe.label(dict(ROW, delivered_gold=0), "delivered") == 0


def test_loco_on_separable_data_is_perfect():
    rows = []
    for corpus in ("a", "b", "c"):
        for i in range(15):
            rows.append(dict(ROW, corpus=corpus, ratio_top2=3.0 + i * 0.01, rank_of_first_gold=1))
            rows.append(dict(ROW, corpus=corpus, ratio_top2=1.0 + i * 0.001, score_gap=0.0001,
                             rank_of_first_gold=7))
    res = probe.loco(rows, "scale_free", "rank1", model="logistic")
    assert res["pooled_auc"] == pytest.approx(1.0)
    assert len(res["folds"]) == 3
