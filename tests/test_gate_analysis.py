"""Tests for the flip-default gate analysis (benchmarks/gate_analysis.py).

The gate that decides whether per_gene_budget may default to "dynamic" hinges
on ONE question: at multi-gene depth, does flipping fixed->dynamic drop any
gene -- especially the gold gene -- that the fixed arm delivered? compare_arms
is the function that answers it, so its set-diff / gold-drop logic is the part
that must be provably correct.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Worktree root on sys.path so `benchmarks` (empty __init__) imports cleanly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.gate_analysis import compare_arms, canon


def _rec(qid, rels, gold):
    return {"id": qid, "delivered_rels": list(rels), "gold_delivered": gold}


# canon() normalizes a source path to its rel-after-sources/ key so gold paths
# (absolute, with .../sources/...) and delivered <GENE src="..."> values (which
# appear either as "sources/X/Y" OR already-relative "X/Y") compare equal. The
# already-relative form is the case the old _rel_after_sources mishandled
# (returned None), which silently zeroed gold matches for those docs.

def test_canon_strips_absolute_sources_prefix():
    assert canon(r"F:\Projects\EnterpriseRAG-Bench-main\generated_data\sources\linear\design\DES-91235.json") \
        == "linear/design/DES-91235.json"


def test_canon_strips_leading_sources():
    assert canon("sources/github/pr-18421.json") == "github/pr-18421.json"


def test_canon_passes_through_already_relative():
    # the form the old helper dropped to None
    assert canon("linear/design/DES-91235.json") == "linear/design/DES-91235.json"


def test_canon_two_forms_of_same_doc_match():
    a = canon(r"F:\Projects\EnterpriseRAG-Bench-main\generated_data\sources\linear\design\DES-91235.json")
    b = canon("linear/design/DES-91235.json")
    assert a == b


def test_canon_empty_is_empty():
    assert canon("") == ""


def test_empty_inputs():
    out = compare_arms([], [])
    assert out["n"] == 0
    assert out["gold_drop_queries"] == []
    assert out["queries_with_drop"] == 0
    assert out["total_genes_fixed"] == 0
    assert out["total_genes_dynamic"] == 0


def test_identical_delivery_no_drop():
    fixed = [_rec("q1", ["a", "b", "c"], True)]
    dynamic = [_rec("q1", ["a", "b", "c"], True)]
    out = compare_arms(fixed, dynamic)
    assert out["n"] == 1
    assert out["queries_with_drop"] == 0
    assert out["queries_with_gain"] == 0
    assert out["gold_drop_queries"] == []
    assert out["total_genes_fixed"] == 3
    assert out["total_genes_dynamic"] == 3


def test_nongold_gene_dropped_is_tolerated():
    # dynamic dropped tail gene "c" (not the gold) -- a drop, but NOT a gold drop.
    fixed = [_rec("q1", ["a", "b", "c"], True)]
    dynamic = [_rec("q1", ["a", "b"], True)]
    out = compare_arms(fixed, dynamic)
    assert out["queries_with_drop"] == 1
    assert out["gold_drop_queries"] == []  # gate still passes on this query
    assert out["dropped_gene_examples"] == [("q1", ["c"])]


def test_gold_gene_dropped_is_the_fail_condition():
    # fixed delivered the gold; dynamic did not -> gate-failing regression.
    fixed = [_rec("q1", ["gold", "x"], True)]
    dynamic = [_rec("q1", ["x"], False)]
    out = compare_arms(fixed, dynamic)
    assert out["gold_drop_queries"] == ["q1"]
    assert out["queries_with_drop"] == 1
    assert out["gold_delivered_fixed"] == 1
    assert out["gold_delivered_dynamic"] == 0


def test_gain_is_flagged_for_sanity():
    # dynamic should never ADD a gene fixed lacked (retrieval is identical,
    # trim only removes) -- if it does, surface it as a sanity flag.
    fixed = [_rec("q1", ["a"], False)]
    dynamic = [_rec("q1", ["a", "b"], False)]
    out = compare_arms(fixed, dynamic)
    assert out["queries_with_gain"] == 1
    assert out["queries_with_drop"] == 0


def test_gold_gain_when_dynamic_delivers_gold_fixed_missed():
    fixed = [_rec("q1", ["x"], False)]
    dynamic = [_rec("q1", ["gold", "x"], True)]
    out = compare_arms(fixed, dynamic)
    assert out["gold_gain_queries"] == ["q1"]
    assert out["gold_drop_queries"] == []


def test_join_is_by_id_and_order_independent():
    fixed = [_rec("q1", ["a"], True), _rec("q2", ["b", "c"], True)]
    dynamic = [_rec("q2", ["b", "c"], True), _rec("q1", ["a"], True)]
    out = compare_arms(fixed, dynamic)
    assert out["n"] == 2
    assert out["gold_drop_queries"] == []
    assert out["total_genes_fixed"] == 3
    assert out["total_genes_dynamic"] == 3


def test_unmatched_ids_are_reported_not_counted():
    fixed = [_rec("q1", ["a"], True), _rec("only_fixed", ["z"], True)]
    dynamic = [_rec("q1", ["a"], True), _rec("only_dyn", ["y"], True)]
    out = compare_arms(fixed, dynamic)
    assert out["n"] == 1  # only q1 is common
    assert set(out["unmatched_ids"]) == {"only_fixed", "only_dyn"}


def test_multiple_gold_drops_collected_in_order():
    fixed = [
        _rec("q1", ["g1"], True),
        _rec("q2", ["g2"], True),
        _rec("q3", ["ok"], True),
    ]
    dynamic = [
        _rec("q1", [], False),
        _rec("q2", [], False),
        _rec("q3", ["ok"], True),
    ]
    out = compare_arms(fixed, dynamic)
    assert out["gold_drop_queries"] == ["q1", "q2"]
    assert out["gold_delivered_fixed"] == 3
    assert out["gold_delivered_dynamic"] == 1
