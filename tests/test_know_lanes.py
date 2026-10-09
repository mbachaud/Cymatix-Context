"""[know] model = "lanes": dense-free know confidence from served inputs (#482)."""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from cymatix_context.config import CymatixConfig, load_config
from cymatix_context.genome import Genome
from cymatix_context.schemas import KnowBlock, MissBlock
from cymatix_context.scoring.know_decision import _decide_know_or_miss_impl
from cymatix_context.scoring.know_lanes import (
    FEATURE_NAMES, LanesModel, feature_values, lane_signals, score_shape,
)

from tests.conftest import make_gene

ROOT = Path(__file__).resolve().parents[1]


def _probe():
    spec = importlib.util.spec_from_file_location(
        "know_feature_probe", ROOT / "benchmarks/dogfood/know/know_feature_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ROWS = [
    {"top_score": 0.4, "score_gap": 0.1, "ratio_top2": 1.33, "pool_size": 50, "coordinate_confidence": 0.5,
     "lane_signals": {"lanes_fired": 3, "top1_lanes": 2, "lanes_top3_agree": 2, "frac_lanes_agree": 0.667,
                      "fts5_top1_is_fused_top1": True, "query_term_coverage": 0.8}},
    {"top_score": 0.15, "score_gap": 0.0, "ratio_top2": 1.0, "pool_size": 1, "coordinate_confidence": None,
     "lane_signals": {"lanes_fired": 0, "top1_lanes": None, "lanes_top3_agree": None, "frac_lanes_agree": None,
                      "fts5_top1_is_fused_top1": None, "query_term_coverage": None}},
    {"top_score": 0.0, "score_gap": 0.0, "ratio_top2": 0.0, "pool_size": 0, "lane_signals": None},
]


@pytest.mark.parametrize("row", ROWS)
def test_features_match_the_probe_that_fit_them(row):
    """Train/serve parity: the served features are the probe's 'agreement' set."""
    served = feature_values(row)
    assert [served[n] for n in FEATURE_NAMES] == pytest.approx(_probe().feature_vector(row, "agreement"))


def test_bench_lane_signals_is_the_package_function():
    spec = importlib.util.spec_from_file_location(
        "lane_signals_bench", ROOT / "benchmarks/dogfood/know/lane_signals.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.lane_signals is lane_signals


def test_score_shape_served_convention():
    assert score_shape({"a": 0.4, "b": 0.3, "c": 0.1}) == pytest.approx(
        {"top_score": 0.4, "score_gap": 0.1, "ratio_top2": 0.4 / 0.3, "pool_size": 3.0})
    assert score_shape({"a": 0.4}) == pytest.approx(
        {"top_score": 0.4, "score_gap": 0.4, "ratio_top2": 0.4, "pool_size": 1.0})


def test_lanes_model_logistic():
    m = LanesModel(intercept=-1.0, betas={"rel_gap": 2.0, "lanes_fired": 0.5})
    z = -1.0 + 2.0 * 0.25 + 0.5 * 3
    assert m.confidence({"rel_gap": 0.25, "lanes_fired": 3}) == pytest.approx(1 / (1 + math.exp(-z)))
    assert LanesModel().confidence({}) == pytest.approx(0.5)


# ── config ────────────────────────────────────────────────────────────

def test_know_model_defaults_to_legacy():
    assert CymatixConfig().know.model == "legacy"


def test_know_lanes_config_is_read(tmp_path, caplog):
    p = tmp_path / "c.toml"
    p.write_text('[know]\nmodel = "lanes"\nlanes_intercept = -1.5\n'
                 'lanes_betas = { rel_gap = 0.8, lanes_fired = 0.2, not_a_feature = 9.0 }\n', encoding="utf-8")
    with caplog.at_level("WARNING"):
        k = load_config(str(p)).know
    assert k.model == "lanes"
    assert k.lanes_intercept == pytest.approx(-1.5)
    assert k.lanes_betas == {"rel_gap": 0.8, "lanes_fired": 0.2}
    assert "not_a_feature" in caplog.text


def test_know_model_invalid_falls_back_to_legacy(tmp_path, caplog):
    p = tmp_path / "c.toml"
    p.write_text('[know]\nmodel = "oracle"\n', encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_config(str(p)).know.model == "legacy"


# ── decision hook ─────────────────────────────────────────────────────

def _window():
    return SimpleNamespace(
        context_health=SimpleNamespace(status="aligned", genes_expressed=3),
        metadata={}, expressed_gene_ids=["g1"],
    )


def _decide(**kw):
    base = dict(query="ledger reconciliation", top_score=0.2, score_gap=0.01,
                lexical_dense_agree=None, coordinate_confidence=0.5)
    base.update(kw)
    return _decide_know_or_miss_impl(_window(), **base)


def test_confidence_override_decides_the_floor():
    hi = _decide(confidence_override=0.9)
    assert isinstance(hi, KnowBlock) and hi.confidence == pytest.approx(0.9)
    lo = _decide(confidence_override=0.1)
    assert isinstance(lo, MissBlock) and lo.reason == "sparse"


def test_override_does_not_bypass_the_freshness_gate():
    top = SimpleNamespace(source_id="/repo/a.md", gene_id="g1")
    out = _decide(confidence_override=0.99, freshness_status="stale", top_gene=top)
    assert isinstance(out, MissBlock) and out.reason == "stale"


# ── served wiring ─────────────────────────────────────────────────────

@pytest.mark.parametrize("model", [None, LanesModel(intercept=3.0)])
def test_packet_path_passes_lanes_confidence(monkeypatch, model):
    import cymatix_context.scoring.know_decision as kd
    import cymatix_context.scoring.know_lanes as kl
    from cymatix_context.context_packet import build_context_packet

    monkeypatch.setattr(kl, "load_lanes_model", lambda *a, **k: model)
    seen = {}
    real = kd.decide_know_or_miss

    def spy(window=None, **kw):
        seen["override"] = kw.get("confidence_override")
        return real(window=window, **kw)

    # _attach_know_or_miss imports decide_know_or_miss at call time.
    monkeypatch.setattr(kd, "decide_know_or_miss", spy)
    genome = Genome(":memory:")
    try:
        genome.upsert_gene(make_gene("Ledger reconciliation runs nightly at two", domains=["ledger"],
                                     entities=["ledger"], gene_id="g-led"), apply_gate=False)
        build_context_packet("ledger reconciliation", genome=genome)
        if model is None:
            assert seen.get("override") is None
        else:
            assert seen["override"] == pytest.approx(1 / (1 + math.exp(-3.0)))
    finally:
        genome.close()


@pytest.mark.parametrize("model,expect_override", [("legacy", False), ("lanes", True)])
def test_helpers_pass_lanes_confidence(monkeypatch, model, expect_override):
    import cymatix_context.server.helpers as helpers

    genome = Genome(":memory:")
    try:
        g = make_gene("Ledger reconciliation runs nightly at two", gene_id="g-led")
        genome.upsert_gene(g, apply_gate=False)
        cfg = CymatixConfig()
        cfg.know.model = model
        cfg.know.lanes_intercept = 3.0
        cfg.know.lanes_betas = {}
        mgr = SimpleNamespace(genome=genome, config=cfg, _mtime_cache={},
                              _cold_tier_peek=lambda q, k=3, min_cosine=0.4: [],
                              _last_cold_peek_targets=[])
        window = SimpleNamespace(
            expressed_gene_ids=["g-led"], retrieval_scores={"g-led": 0.4},
            tier_contributions={"g-led": {"fts5": 0.4}},
            context_health=SimpleNamespace(freshness_min=None, genes_expressed=1, status="aligned"),
        )
        seen = {}
        real = helpers.decide_know_or_miss

        def spy(window=None, **kw):
            seen["override"] = kw.get("confidence_override")
            return real(window=window, **kw)

        monkeypatch.setattr(helpers, "decide_know_or_miss", spy)
        helpers._compute_know_or_miss_block(cymatix=mgr, window=window, query="ledger reconciliation")
        if expect_override:
            assert seen["override"] == pytest.approx(1 / (1 + math.exp(-3.0)))
        else:
            assert seen.get("override") is None
    finally:
        genome.close()
