"""Served cross-encoder input for the [know] lanes model, opt-in (#482).

The 30-bed CE replays (cymatix-receipts 85dc1bb) lift pooled LOCO AUC 0.743 ->
0.823 and let a store-calibrated know fire on ERB (15.5% @ 0.77 held out).
``[know] lanes_ce_model`` (empty = off) makes the server compute the same
ce_top1/ce_margin features the bench fit used.
"""
from __future__ import annotations

import importlib.util
import json
import math
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from cymatix_context.config import CymatixConfig, load_config
from cymatix_context.genome import Genome
from cymatix_context.scoring import know_lanes as kl
from cymatix_context.scoring.know_lanes import LanesModel, feature_values, load_lanes_model, served_lanes_confidence

from tests.conftest import make_gene

ROOT = Path(__file__).resolve().parents[1]
SCORES = {"g1": 0.4, "g2": 0.3, "g3": 0.1}
TEXTS = {"g1": "ledger reconciliation nightly", "g2": "payroll export", "g3": "misc"}


def _served(model, scorer=None):
    return served_lanes_confidence(model, scores=SCORES, tier_contributions={"g1": {"fts5": 1.0}},
                                   query="ledger reconciliation", top1_text=TEXTS["g1"],
                                   coordinate_confidence=0.5, text_of=TEXTS.get, ce_scorer=scorer)


def test_ce_model_scores_top1_and_top2_and_feeds_the_features():
    calls = []

    def scorer(q, texts):
        calls.append(list(texts))
        return [4.0, 1.5]

    m = LanesModel(-1.0, {"ce_top1": 0.5, "ce_margin": 0.2, "rel_gap": 1.0}, ce_model="any-ce")
    got = _served(m, scorer)
    assert calls == [[TEXTS["g1"], TEXTS["g2"]]]
    z = -1.0 + 0.5 * 4.0 + 0.2 * 2.5 + 1.0 * ((0.4 - 0.3) / 0.4)
    assert got == pytest.approx(1 / (1 + math.exp(-z)))


def test_no_ce_model_never_calls_a_scorer():
    def boom(q, t):
        raise AssertionError("scorer must not run without lanes_ce_model")

    m = LanesModel(-1.0, {"rel_gap": 1.0})
    row = dict(kl.score_shape(SCORES), coordinate_confidence=0.5,
               lane_signals=kl.lane_signals({"g1": {"fts5": 1.0}}, kl.fused_order(SCORES),
                                            query="ledger reconciliation", top1_text=TEXTS["g1"]))
    assert _served(m, boom) == pytest.approx(m.confidence(feature_values(row)))


def test_ce_failure_reads_as_missing_not_error():
    def boom(q, t):
        raise RuntimeError("model not installed")

    m = LanesModel(0.0, {"ce_top1_missing": 2.0}, ce_model="any-ce")
    assert _served(m, boom) == pytest.approx(1 / (1 + math.exp(-2.0)))


def test_config_lanes_ce_model_and_ce_betas(tmp_path):
    assert CymatixConfig().know.lanes_ce_model == ""
    p = tmp_path / "c.toml"
    p.write_text('[know]\nmodel = "lanes"\nlanes_ce_model = "cross-encoder/ms-marco-MiniLM-L-6-v2"\n'
                 'lanes_betas = { ce_top1 = 0.7, ce_margin_missing = -0.3, rel_gap = 0.1 }\n', encoding="utf-8")
    k = load_config(str(p)).know
    assert k.lanes_ce_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert k.lanes_betas == {"ce_top1": 0.7, "ce_margin_missing": -0.3, "rel_gap": 0.1}
    assert load_lanes_model(p).ce_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"


def test_helpers_use_the_configured_ce_model(monkeypatch):
    import cymatix_context.backends.rerank_backend as rb
    import cymatix_context.server.helpers as helpers

    seen_models = []

    def fake_score_pairs(q, texts, *, model_name=None, **kw):
        seen_models.append(model_name)
        return [3.0] * len(texts)

    monkeypatch.setattr(rb, "score_pairs", fake_score_pairs)
    genome = Genome(":memory:")
    try:
        genome.upsert_gene(make_gene("Ledger reconciliation runs nightly", gene_id="g-led"), apply_gate=False)
        cfg = CymatixConfig()
        cfg.know.model, cfg.know.lanes_intercept = "lanes", 0.0
        cfg.know.lanes_betas = {"ce_top1": 1.0}
        cfg.know.lanes_ce_model = "my-ce"
        mgr = SimpleNamespace(genome=genome, config=cfg, _mtime_cache={},
                              _cold_tier_peek=lambda q, k=3, min_cosine=0.4: [], _last_cold_peek_targets=[])
        window = SimpleNamespace(expressed_gene_ids=["g-led"], retrieval_scores={"g-led": 0.4},
                                 tier_contributions={"g-led": {"fts5": 0.4}},
                                 context_health=SimpleNamespace(freshness_min=None, genes_expressed=1,
                                                                status="aligned"))
        seen = {}
        real = helpers.decide_know_or_miss

        def spy(window=None, **kw):
            seen["override"] = kw.get("confidence_override")
            return real(window=window, **kw)

        monkeypatch.setattr(helpers, "decide_know_or_miss", spy)
        helpers._compute_know_or_miss_block(cymatix=mgr, window=window, query="ledger")
        assert seen_models == ["my-ce"]
        assert seen["override"] == pytest.approx(1 / (1 + math.exp(-3.0)))
    finally:
        genome.close()


def _fit():
    spec = importlib.util.spec_from_file_location("fit_know_lanes", ROOT / "scripts" / "fit_know_lanes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fit_ce_writes_a_loadable_toml(tmp_path):
    pytest.importorskip("sklearn")
    paths = []
    for c, seed in (("a", 1), ("b", 2), ("c", 3)):
        rng = random.Random(seed)
        rows = [{"needle": f"{c}{i}", "top_score": 0.3, "score_gap": 0.05, "ratio_top2": 1.2, "pool_size": 30,
                 "rank_of_first_gold": 1 if ce > 0.5 else 4, "lane_signals": {"lanes_fired": 2},
                 "ce": {"ce_top1": ce, "ce_margin": abs(ce) / 2}}
                for i, ce in enumerate(rng.gauss(0, 2) for _ in range(200))]
        p = tmp_path / f"know_replay_{c}_2026-10-08.json"
        p.write_text(json.dumps({"ce_model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
                                 "arms": [{"arm": "postflip_default", "per_query": rows}]}), encoding="utf-8")
        paths += ["--receipt", f"{c}={p}"]
    tom = tmp_path / "t.toml"
    assert _fit().main([*paths, "--features", "ce", "--out", str(tmp_path / "r.json"), "--toml-out", str(tom)]) == 0
    k = load_config(str(tom)).know
    assert k.model == "lanes" and k.lanes_ce_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert "ce_top1" in k.lanes_betas
