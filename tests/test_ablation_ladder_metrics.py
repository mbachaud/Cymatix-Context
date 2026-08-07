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


# ── #341 review fix: per-request ring correlation (mark + since-mark) ────
#
# context_manager._pipeline_events is a process-lifetime global deque.
# Draining "the globally newest splice entry" with no correlation to the
# CURRENT build_context call leaks a prior query's (or the untimed
# warmup's) splice count into any query whose own build_context never
# rings a splice entry (no-match early return, abstain, pre-splice
# exception). pipeline_ring_mark/events_since_mark fix this: mark the ring
# immediately before a call, then only accept entries strictly newer than
# that mark.


def test_pipeline_ring_mark_of_empty_ring_is_zero():
    lad = _load_ladder()
    assert lad.pipeline_ring_mark([]) == 0.0


def test_pipeline_ring_mark_is_newest_ts():
    lad = _load_ladder()
    events = [{"stage": "express", "ts": 10.0}, {"stage": "splice", "ts": 12.5},
              {"stage": "assemble", "ts": 11.0}]
    assert lad.pipeline_ring_mark(events) == 12.5


def test_events_since_mark_excludes_entries_at_or_before_mark():
    lad = _load_ladder()
    events = [{"stage": "splice", "ts": 5.0, "n_candidates": 1},
              {"stage": "express", "ts": 8.0},
              {"stage": "splice", "ts": 9.0, "n_candidates": 2}]
    since = lad.events_since_mark(events, 8.0)
    assert since == [{"stage": "splice", "ts": 9.0, "n_candidates": 2}]


def test_mark_and_since_mark_prevent_stale_splice_leak_across_needles():
    """The exact scenario the review flagged: needle 1 rings a splice
    entry; needle 2's build_context never reaches splice (simulated
    no-match/abstain). Draining the newest splice entry in the WHOLE ring
    after needle 2 would wrongly return needle 1's count. Marking the ring
    before each call and filtering to entries since that mark must return
    None for needle 2 instead."""
    lad = _load_ladder()
    ring: list = []

    # Needle 1: mark the (empty) ring, then its build_context rings splice.
    mark_1 = lad.pipeline_ring_mark(ring)
    ring.append({"stage": "express", "ts": 1.0})
    ring.append({"stage": "splice", "ts": 1.5, "n_candidates": 4})
    n1_count = lad.newest_splice_count(lad.events_since_mark(ring, mark_1))
    assert n1_count == 4

    # Needle 2: mark AFTER needle 1's entries, then its build_context rings
    # no splice entry at all (only express/assemble — abstain path).
    mark_2 = lad.pipeline_ring_mark(ring)
    ring.append({"stage": "express", "ts": 2.0})
    ring.append({"stage": "assemble", "ts": 2.2})
    n2_count = lad.newest_splice_count(lad.events_since_mark(ring, mark_2))
    assert n2_count is None, (
        "needle 2 never rang splice -- must not inherit needle 1's count"
    )

    # Sanity: draining the WHOLE ring (the pre-fix bug) would have leaked
    # needle 1's count into needle 2 -- confirms the scenario is real.
    assert lad.newest_splice_count(ring) == 4
