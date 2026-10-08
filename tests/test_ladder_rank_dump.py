"""Opt-in ``--rank-dump N`` for the ablation ladder (issue #482, BEIR round 1).

BEIR-standard NDCG@10 needs the ranked list itself, not only the ranks of the
gold genes: graded qrels need to know WHICH gold gene sits at each rank, the
ArguAna-style self-document must be removed from the ranking before scoring,
and document-level NDCG must collapse several genes of one document. The dump
records the top-N gene ids per needle on both bases (score map and final
order). Off by default, and the receipt shape is unchanged when off.
"""

import importlib.util
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_ladder():
    spec = importlib.util.spec_from_file_location(
        "ablation_ladder", ROOT / "benchmarks" / "dogfood" / "erb" / "ablation_ladder.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ladder = _load_ladder()


def test_score_order_matches_rank_gold_ordering_and_tiebreak():
    scores = {"g_b": 2.0, "g_a": 2.0, "g_c": 5.0, "g_d": 1.0}
    assert ladder.score_order(scores) == ["g_c", "g_a", "g_b", "g_d"]
    # Same order rank_gold uses: g_b sits at rank 3 in both.
    _, ranks = ladder.rank_gold(scores, {"g_b"})
    assert ranks == [ladder.score_order(scores).index("g_b") + 1]


def test_score_order_truncates_to_depth():
    scores = {f"g{i:02d}": float(i) for i in range(30)}
    out = ladder.score_order(scores, depth=5)
    assert out == ["g29", "g28", "g27", "g26", "g25"]


def test_rank_dump_fields_off_emits_nothing():
    assert ladder.rank_dump_fields({"a": 1.0}, ["a"], depth=0) == {}


def test_rank_dump_fields_records_both_bases_truncated():
    scores = {"a": 3.0, "b": 2.0, "c": 1.0}
    final = ["c", "a", "b"]
    out = ladder.rank_dump_fields(scores, final, depth=2)
    assert out == {"score_ranked_ids": ["a", "b"], "final_ranked_ids": ["c", "a"]}


def test_rank_dump_fields_handles_empty_inputs():
    out = ladder.rank_dump_fields({}, [], depth=10)
    assert out == {"score_ranked_ids": [], "final_ranked_ids": []}


def test_run_arm_accepts_rank_dump_defaulting_off():
    param = inspect.signature(ladder.run_arm).parameters["rank_dump"]
    assert param.default == 0
