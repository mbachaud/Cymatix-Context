"""Unit tests for #335 delivered-gold metrics in the ERB ablation ladder.

Scope is the pure helpers only — no beds, no manager construction. See
``tests/test_ablation_tools.py`` for the broader ablation-ladder helper
coverage; these are grouped separately because they land with #335 first.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_ladder():
    # ablation_ladder.py defines a frozen @dataclass (Arm); the dataclasses
    # module resolves ClassVar string annotations via
    # sys.modules.get(cls.__module__), so the module must be registered in
    # sys.modules BEFORE exec_module runs, or that lookup returns None and
    # dataclass construction blows up with an unrelated AttributeError. See
    # the working loader at tests/test_ablation_tools.py:24-42.
    spec = importlib.util.spec_from_file_location(
        "ablation_ladder", ROOT / "benchmarks" / "dogfood" / "erb" / "ablation_ladder.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_delivered_gold_fields_hit():
    lad = _load_ladder()
    out = lad.delivered_gold_fields(["a", "b", "gold1", "c"], {"gold1", "gold9"})
    assert out == {"delivered_gold": 1, "delivered_count": 4, "delivered_gold_rank": 3}


def test_delivered_gold_fields_miss_and_empty():
    lad = _load_ladder()
    assert lad.delivered_gold_fields(["a", "b"], {"g"}) == {
        "delivered_gold": 0, "delivered_count": 2, "delivered_gold_rank": None}
    assert lad.delivered_gold_fields([], {"g"})["delivered_count"] == 0


def test_arm_level_delivered_gold_rate():
    lad = _load_ladder()
    recs = [{"delivered_gold": 1}, {"delivered_gold": 0}, {"delivered_gold": 1}]
    assert lad.delivered_gold_rate(recs) == 2 / 3
    assert lad.delivered_gold_rate([]) == 0.0


def test_newest_splice_count_returns_last_splice_entry():
    lad = _load_ladder()
    events = [
        {"stage": "express", "n_candidates": 99},
        {"stage": "splice", "n_candidates": 3},
        {"stage": "assemble"},
        {"stage": "splice", "n_candidates": 5},
    ]
    assert lad.newest_splice_count(events) == 5


def test_newest_splice_count_none_when_splice_never_ran():
    lad = _load_ladder()
    events = [{"stage": "express", "n_candidates": 99}, {"stage": "assemble"}]
    assert lad.newest_splice_count(events) is None
    assert lad.newest_splice_count([]) is None
