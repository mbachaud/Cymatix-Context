"""Dense-free agreement signals for know/miss (issue #482).

lexical_dense_agree needs a dense lane; with dense off it is unknowable. These
signals ask the same question of the lanes that DO run: do several
independent evidence lanes back the fused top-1? Pure functions over the
pipeline's own per-candidate tier map, fused order, query and top-1 text.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "lane_signals", ROOT / "benchmarks" / "dogfood" / "know" / "lane_signals.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ls = _load()

TIERS = {
    "A": {"fts5": 3.0, "tag_exact": 1.0, "access_rate": 9.0},
    "B": {"fts5": 2.0, "tag_prefix": 5.0},
    "C": {"fts5": 1.0, "tag_exact": 2.0, "filename_anchor": 1.0},
    "D": {"tag_prefix": 1.0},
}
FUSED = ["A", "B", "C", "D"]


def test_boost_tiers_are_not_evidence_lanes():
    assert "access_rate" not in ls.EVIDENCE_LANES
    assert {"fts5", "tag_exact", "tag_prefix", "filename_anchor"} <= ls.EVIDENCE_LANES


def test_lane_counts_for_the_fused_top1():
    s = ls.lane_signals(TIERS, FUSED, query="alpha beta", top1_text=None, k=3)
    assert s["lanes_fired"] == 4            # fts5, tag_exact, tag_prefix, filename_anchor
    assert s["top1_lanes"] == 2             # A has fts5 + tag_exact (access_rate is a boost)
    # Own top-3 per lane: fts5 A,B,C (has A); tag_exact C,A (has A);
    # tag_prefix B,D (no A); filename_anchor C (no A).
    assert s["lanes_top3_agree"] == 2
    assert s["frac_lanes_agree"] == pytest.approx(0.5)
    assert s["fts5_top1_is_fused_top1"] is True


def test_fts5_flag_is_unknown_when_fts5_did_not_fire():
    s = ls.lane_signals({"A": {"tag_exact": 1.0}}, ["A"], query="q", top1_text=None)
    assert s["fts5_top1_is_fused_top1"] is None
    assert s["frac_lanes_agree"] == pytest.approx(1.0)


def test_empty_inputs_are_unknown_not_zero():
    s = ls.lane_signals({}, [], query="q", top1_text=None)
    assert s["lanes_fired"] == 0
    assert s["top1_lanes"] is None and s["lanes_top3_agree"] is None
    assert s["frac_lanes_agree"] is None and s["query_term_coverage"] is None


def test_query_term_coverage_ignores_short_terms_and_case():
    s = ls.lane_signals(TIERS, FUSED, query="How does the Splice step work?",
                        top1_text="the splice STEP compresses each candidate", k=3)
    # terms >= 3 chars, stopwords removed: splice, step, work -> splice, step present
    assert s["query_term_coverage"] == pytest.approx(2 / 3)


def test_coverage_is_unknown_without_top1_text_or_terms():
    assert ls.lane_signals(TIERS, FUSED, query="the of a", top1_text="x")["query_term_coverage"] is None
    assert ls.lane_signals(TIERS, FUSED, query="splice", top1_text=None)["query_term_coverage"] is None
