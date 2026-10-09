"""Per-store calibration of the [know] lanes model (#482).

The lanes model ranks well across corpora but its absolute scale does not
transfer (ERB: held-out AUC 0.74, max confidence 0.36 < floor 0.45). A store
recalibrates it on its own labelled queries: Platt rescaling of the lanes
logit (``lanes_platt_a``/``lanes_platt_b``, identity by default) plus an
``emit_floor`` chosen by cross-validation. ``scripts/calibrate_know_store.py``
writes the block, and refuses to write a floor whose held-out precision misses
the target.
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
from cymatix_context.scoring.know_lanes import LanesModel, load_lanes_model

from tests.conftest import make_gene

ROOT = Path(__file__).resolve().parents[1]
FEATS = {"rel_gap": 0.3, "lanes_fired": 2.0, "query_term_coverage": 0.5}


def test_identity_platt_is_byte_identical():
    m = LanesModel(intercept=-0.5, betas={"rel_gap": 1.2, "lanes_fired": 0.3})
    assert LanesModel(-0.5, {"rel_gap": 1.2, "lanes_fired": 0.3}, platt_a=1.0, platt_b=0.0).confidence(FEATS) \
        == m.confidence(FEATS)


def test_platt_rescales_the_logit():
    m = LanesModel(intercept=-0.5, betas={"rel_gap": 1.2}, platt_a=2.0, platt_b=0.7)
    z = -0.5 + 1.2 * 0.3
    assert m.confidence(FEATS) == pytest.approx(1 / (1 + math.exp(-(2.0 * z + 0.7))))


def test_platt_defaults_and_toml(tmp_path):
    k = CymatixConfig().know
    assert (k.lanes_platt_a, k.lanes_platt_b) == (1.0, 0.0)
    p = tmp_path / "c.toml"
    p.write_text('[know]\nmodel = "lanes"\nlanes_platt_a = 1.8\nlanes_platt_b = 2.5\n', encoding="utf-8")
    m = load_lanes_model(p)
    assert (m.platt_a, m.platt_b) == (1.8, 2.5)


def test_helpers_apply_store_platt(monkeypatch):
    import cymatix_context.server.helpers as helpers

    genome = Genome(":memory:")
    try:
        genome.upsert_gene(make_gene("Ledger reconciliation runs nightly", gene_id="g-led"), apply_gate=False)
        cfg = CymatixConfig()
        cfg.know.model, cfg.know.lanes_intercept, cfg.know.lanes_betas = "lanes", 0.5, {}
        cfg.know.lanes_platt_a, cfg.know.lanes_platt_b = 2.0, 1.0
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
        assert seen["override"] == pytest.approx(1 / (1 + math.exp(-(2.0 * 0.5 + 1.0))))
    finally:
        genome.close()


# ── scripts/calibrate_know_store.py ──────────────────────────────────

def _script():
    spec = importlib.util.spec_from_file_location("calibrate_know_store",
                                                  ROOT / "scripts" / "calibrate_know_store.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _replay(tmp_path, n, signal, seed):
    """Rows whose query_term_coverage predicts rank-1 gold with strength `signal`."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        cov = rng.random()
        hit = rng.random() < (0.1 + signal * cov)
        rows.append({"needle": f"n{i}", "top_score": 0.3, "score_gap": 0.05, "ratio_top2": 1.2, "pool_size": 40,
                     "coordinate_confidence": 0.5, "rank_of_first_gold": 1 if hit else 3,
                     "lane_signals": {"lanes_fired": 3, "top1_lanes": 2, "lanes_top3_agree": 2,
                                      "frac_lanes_agree": 0.67, "fts5_top1_is_fused_top1": True,
                                      "query_term_coverage": cov}})
    p = tmp_path / f"know_replay_store_{seed}.json"
    p.write_text(json.dumps({"arms": [{"arm": "postflip_default", "per_query": rows}]}), encoding="utf-8")
    return p


def _lanes_toml(tmp_path):
    p = tmp_path / "lanes.toml"
    p.write_text('[know]\nmodel = "lanes"\nlanes_intercept = -1.0\n'
                 'lanes_betas = { query_term_coverage = 2.0 }\n', encoding="utf-8")
    return p


@pytest.mark.parametrize("center,spread,true_a,true_b,seed", [
    (-3.6, 0.9, 0.85, 2.0, 7),    # ERB-like: low-confidence scores far from the identity start
    (0.0, 1.5, 1.3, -0.4, 8),
    (2.5, 0.5, 2.0, -5.5, 9),
])
def test_fit_platt_converges_like_a_reference_logistic(center, spread, true_a, true_b, seed):
    """Regression (2026-10-09): undamped Newton from (1, 0) diverged to a~8e8 on
    ERB's CE-lanes logits (z ~ -3.6), collapsing every probability to 0 so the
    script reported no_usable_floor where a usable floor exists."""
    mod = _script()
    rng = random.Random(seed)
    z = [rng.gauss(center, spread) for _ in range(470)]
    y = [1 if rng.random() < 1 / (1 + math.exp(-(true_a * v + true_b))) else 0 for v in z]
    a, b = mod.fit_platt(z, y)
    assert math.isfinite(a) and math.isfinite(b) and abs(a) < 50
    # Same maximum-likelihood optimum as an unregularised reference fit.
    nll = lambda aa, bb: -sum(math.log(max(1e-15, (1 / (1 + math.exp(-(aa * v + bb)))) if t
                                           else 1 - 1 / (1 + math.exp(-(aa * v + bb))))) for v, t in zip(z, y))
    ref = min(((aa / 20, bb / 10) for aa in range(-20, 100) for bb in range(-80, 60)), key=lambda p: nll(*p))
    assert nll(a, b) <= nll(*ref) + 1e-6


def test_calibrate_writes_a_floor_that_holds_out_of_sample(tmp_path):
    mod = _script()
    rep, out = tmp_path / "rep.json", tmp_path / "store.toml"
    rc = mod.main(["--receipt", str(_replay(tmp_path, 800, 0.85, 1)), "--config", str(_lanes_toml(tmp_path)),
                   "--target-precision", "0.7", "--out", str(rep), "--toml-out", str(out)])
    assert rc == 0
    report = json.loads(rep.read_text(encoding="utf-8"))
    cv = report["cross_validation"]
    assert cv["held_out_precision"] >= 0.7 and cv["held_out_coverage"] > 0.05
    k = load_config(str(out)).know
    assert k.model == "lanes" and k.lanes_platt_a != 1.0 and 0.0 < k.emit_floor < 1.0


def test_calibrate_refuses_when_held_out_precision_misses(tmp_path):
    mod = _script()
    rep, out = tmp_path / "rep.json", tmp_path / "store.toml"
    rc = mod.main(["--receipt", str(_replay(tmp_path, 400, 0.05, 2)), "--config", str(_lanes_toml(tmp_path)),
                   "--target-precision", "0.8", "--out", str(rep), "--toml-out", str(out)])
    assert rc == 2
    assert json.loads(rep.read_text(encoding="utf-8"))["verdict"] == "no_usable_floor"
    assert not out.exists()
