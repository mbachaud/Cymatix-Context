"""Unit tests for the #260 semantic-arm probe pure helpers.

Covers the gold-matching core (id delivery, pool presence, best rank,
gold-answer text overlap), the aggregation math, the sweep<->scored type join,
and the per-arm config mutation. No bed / no model load -- all synthetic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the benchmarks package importable (mirrors tests/test_snow_bench.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.ab_semantic_probe import (  # noqa: E402
    ARMS,
    aggregate,
    apply_arm,
    attach_types,
    best_gold_rank,
    build_cells,
    content_tokens,
    emitted_order,
    gold_answer_overlap,
    normalize_text,
    score_question,
)


# ── text helpers ──────────────────────────────────────────────────────
def test_normalize_text_collapses_ws_and_lowers():
    assert normalize_text("  Foo\t Bar\nBaz ") == "foo bar baz"
    assert normalize_text(None) == ""


def test_content_tokens_drops_stopwords_and_short():
    toks = content_tokens("The default MAX_file_size is 10")
    assert "default" in toks
    assert "max_file_size" in toks  # underscore kept, length>2
    assert "the" not in toks        # stopword
    assert "is" not in toks         # stopword + short


# ── emitted_order + best_gold_rank ────────────────────────────────────
def test_emitted_order_score_desc_then_gene_id_tiebreak():
    scored = {"b": 1.0, "a": 1.0, "c": 2.0}
    # c highest; a before b on the (-score, gene_id) tie-break
    assert emitted_order(scored) == ["c", "a", "b"]


def test_best_gold_rank_first_gold_1based():
    order = ["x", "g2", "y", "g1"]
    assert best_gold_rank(order, ["g1", "g2"]) == 2  # g2 is earlier
    assert best_gold_rank(order, ["g1"]) == 4
    assert best_gold_rank(order, ["missing"]) is None
    assert best_gold_rank([], ["g1"]) is None


# ── gold-answer text overlap ──────────────────────────────────────────
def test_gold_answer_overlap_ratio_and_none():
    # gold tokens: {default, limit, mib} -> assembled has default+limit
    ov = gold_answer_overlap("default limit MiB", "the default limit was set")
    assert ov == pytest.approx(2 / 3)
    assert gold_answer_overlap(None, "anything") is None
    assert gold_answer_overlap("", "anything") is None


# ── score_question bundle ─────────────────────────────────────────────
def test_score_question_delivered_and_ranked():
    scored = {"g1": 5.0, "n1": 4.0, "n2": 3.0}
    m = score_question(
        gold_ids=["g1"], expressed_ids=["g1", "n1"], scored=scored,
        gold_answer="alpha beta", assembled="alpha beta gamma",
    )
    assert m["gold_delivered_id"] is True
    assert m["pool_present"] is True
    assert m["best_gold_rank"] == 1
    assert m["gold_delivered_text"] is True     # overlap 1.0 >= 0.6
    assert m["n_gold_ids"] == 1 and m["pool_size"] == 3


def test_score_question_rank_miss_vs_recall_miss():
    # gold in pool but NOT delivered (rank-miss): present but low-ranked.
    scored = {"n1": 9.0, "n2": 8.0, "g1": 1.0}
    m = score_question(["g1"], ["n1", "n2"], scored, None, "")
    assert m["pool_present"] is True          # recall OK
    assert m["gold_delivered_id"] is False    # but not delivered
    assert m["best_gold_rank"] == 3
    assert m["gold_answer_overlap"] is None    # no gold answer -> soft signal off

    # gold absent from pool entirely (recall-miss).
    m2 = score_question(["g1"], [], {"n1": 1.0}, None, "")
    assert m2["pool_present"] is False
    assert m2["best_gold_rank"] is None


# ── aggregate ─────────────────────────────────────────────────────────
def test_aggregate_rates_and_rank_stats_over_pool_present_only():
    recs = [
        {"gold_delivered_id": True,  "pool_present": True,  "best_gold_rank": 2,
         "gold_delivered_text": True,  "gold_answer_overlap": 0.8},
        {"gold_delivered_id": False, "pool_present": True,  "best_gold_rank": 10,
         "gold_delivered_text": False, "gold_answer_overlap": 0.4},
        {"gold_delivered_id": False, "pool_present": False, "best_gold_rank": None,
         "gold_delivered_text": False, "gold_answer_overlap": None},
    ]
    a = aggregate(recs)
    assert a["n"] == 3
    assert a["gold_delivered_id_rate"] == pytest.approx(1 / 3)
    assert a["pool_present_rate"] == pytest.approx(2 / 3)
    assert a["gold_delivered_text_rate"] == pytest.approx(1 / 3)
    # rank stats only over the two pool-present rows (2 and 10).
    assert a["n_ranked"] == 2
    assert a["mean_best_gold_rank"] == pytest.approx(6.0)
    assert a["median_best_gold_rank"] == pytest.approx(6.0)
    assert a["mean_gold_answer_overlap"] == pytest.approx(0.6)


def test_aggregate_empty():
    a = aggregate([])
    assert a["n"] == 0
    assert a["gold_delivered_id_rate"] is None
    assert a["mean_best_gold_rank"] is None


# ── attach_types join ─────────────────────────────────────────────────
def test_attach_types_joins_by_normalized_text_with_unknown_fallback():
    sweep = [
        {"query": "What Is  The Limit?", "gold_ids": ["g1"]},
        {"query": "Unmatched question", "gold_ids": ["g2"]},
    ]
    scored = [
        {"question": "what is the limit?", "type": "semantic",
         "gold_answer": "10 MiB"},
    ]
    out = attach_types(sweep, scored)
    assert out[0]["type"] == "semantic"
    assert out[0]["gold_answer"] == "10 MiB"
    assert out[0]["gold_ids"] == ["g1"]
    assert out[1]["type"] == "unknown"
    assert out[1]["gold_answer"] is None


# ── apply_arm config mutation ─────────────────────────────────────────
class _Retrieval:
    fusion_mode = "additive"
    dense_embedding_enabled = True
    fts5_weight = 3.0
    tag_exact_weight = 3.0
    tag_prefix_weight = 2.0
    filename_anchor_weight = 4.0
    lex_anchor_weight = 1.0
    bm25_shortlist_enabled = True


class _Ingestion:
    splade_enabled = True


class _Cfg:
    def __init__(self):
        self.retrieval = _Retrieval()
        self.ingestion = _Ingestion()


def test_apply_arm_lexical():
    cfg = _Cfg()
    apply_arm(cfg, "lexical")
    assert cfg.retrieval.fusion_mode == "rrf"
    assert cfg.retrieval.dense_embedding_enabled is False
    assert cfg.ingestion.splade_enabled is False


def test_apply_arm_dense_zeroes_lexical_tiers():
    cfg = _Cfg()
    apply_arm(cfg, "dense")
    assert cfg.retrieval.dense_embedding_enabled is True
    assert cfg.ingestion.splade_enabled is False
    assert cfg.retrieval.fts5_weight == 0.0
    assert cfg.retrieval.tag_exact_weight == 0.0
    assert cfg.retrieval.tag_prefix_weight == 0.0
    assert cfg.retrieval.filename_anchor_weight == 0.0
    assert cfg.retrieval.lex_anchor_weight == 0.0
    assert cfg.retrieval.bm25_shortlist_enabled is False


def test_apply_arm_fused_full_stack():
    cfg = _Cfg()
    apply_arm(cfg, "fused")
    assert cfg.retrieval.fusion_mode == "rrf"
    assert cfg.retrieval.dense_embedding_enabled is True
    assert cfg.ingestion.splade_enabled is True


def test_apply_arm_rejects_unknown():
    with pytest.raises(ValueError):
        apply_arm(_Cfg(), "bogus")


# ── build_cells ───────────────────────────────────────────────────────
def test_build_cells_plain_and_combinator_riders():
    assert [c.name for c in build_cells(["lexical", "fused"], [], 0.05)] == [
        "lexical", "fused"]
    cells = build_cells(["dense"], ["additive", "off"], 0.05)
    assert [c.name for c in cells] == ["dense/additive", "dense/off"]
    assert cells[0].combinator == "additive" and cells[0].arm == "dense"


def test_arms_constant():
    assert ARMS == ("lexical", "dense", "fused")
