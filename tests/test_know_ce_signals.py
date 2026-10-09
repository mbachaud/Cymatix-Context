"""Cross-encoder top-1 signals for the lanes model, bench-first (#482)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from cymatix_context.scoring.know_lanes import (
    CE_FEATURES, FEATURE_NAMES, ce_signals, feature_values,
)

ROOT = Path(__file__).resolve().parents[1]


def test_ce_signals_scores_top1_and_margin():
    texts = {"a": "alpha text", "b": "beta text", "c": "gamma"}
    calls = []

    def scorer(query, docs):
        calls.append((query, list(docs)))
        return [{"alpha text": 3.0, "beta text": 1.25}[d] for d in docs]

    out = ce_signals("q", ["a", "b", "c"], texts.get, scorer)
    assert out == {"ce_top1": 3.0, "ce_margin": 1.75}
    assert calls == [("q", ["alpha text", "beta text"])]


def test_ce_signals_unmeasurable_is_none_not_zero():
    assert ce_signals("q", [], {}.get, lambda q, d: []) == {"ce_top1": None, "ce_margin": None}
    assert ce_signals("q", ["a"], {"a": "x"}.get, lambda q, d: [2.0]) == {"ce_top1": 2.0, "ce_margin": None}
    assert ce_signals("q", ["a", "b"], {}.get, lambda q, d: []) == {"ce_top1": None, "ce_margin": None}


def test_ce_scorer_failure_is_unmeasurable():
    def boom(q, d):
        raise RuntimeError("no model")

    assert ce_signals("q", ["a", "b"], {"a": "x", "b": "y"}.get, boom) == {"ce_top1": None, "ce_margin": None}


def test_ce_features_are_extra_and_flagged():
    assert not set(CE_FEATURES) & set(FEATURE_NAMES)
    base = {"top_score": 0.4, "score_gap": 0.1, "ratio_top2": 1.3, "pool_size": 10, "lane_signals": {}}
    f0 = feature_values(base)
    assert not set(CE_FEATURES) & set(f0)  # base rows: unchanged feature set
    f1 = feature_values(dict(base, ce={"ce_top1": 2.5, "ce_margin": None}), with_ce=True)
    assert (f1["ce_top1"], f1["ce_top1_missing"], f1["ce_margin"], f1["ce_margin_missing"]) == (2.5, 0.0, 0.0, 1.0)
    f2 = feature_values(base, with_ce=True)
    assert (f2["ce_top1_missing"], f2["ce_margin_missing"]) == (1.0, 1.0)


def test_fit_script_ce_feature_set(tmp_path):
    pytest.importorskip("sklearn")
    import json
    import random

    spec = importlib.util.spec_from_file_location("fit_know_lanes", ROOT / "scripts" / "fit_know_lanes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    paths = []
    for c, seed in (("a", 1), ("b", 2), ("c", 3)):
        rng = random.Random(seed)
        rows = []
        for i in range(200):
            ce = rng.gauss(0, 2)
            rows.append({"needle": f"{c}{i}", "top_score": 0.3, "score_gap": 0.05, "ratio_top2": 1.2,
                         "pool_size": 30, "rank_of_first_gold": 1 if ce > 0.5 else 4,
                         "lane_signals": {"lanes_fired": 2}, "ce": {"ce_top1": ce, "ce_margin": abs(ce) / 2}})
        p = tmp_path / f"know_replay_{c}_2026-10-08.json"
        p.write_text(json.dumps({"ce_model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
                                 "arms": [{"arm": "postflip_default", "per_query": rows}]}), encoding="utf-8")
        paths += ["--receipt", f"{c}={p}"]
    rep, tom = tmp_path / "r.json", tmp_path / "t.toml"
    assert mod.main([*paths, "--features", "ce", "--out", str(rep), "--toml-out", str(tom)]) == 0
    report = json.loads(rep.read_text(encoding="utf-8"))
    assert report["features"] == "ce" and "ce_top1" in report["feature_names"]
    assert report["leave_one_corpus_out"]["pooled_auc"] > 0.9
