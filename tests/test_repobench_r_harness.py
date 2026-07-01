"""Unit tests for the RepoBench-R harness logic (no network, no server, no GPU).

Tests cover:
  - acc_at: the core scoring function
  - tok: identifier tokenizer
  - rank_overlap: Jaccard-overlap ranker
  - rank_bm25: BM25Okapi ranker on a tiny pool
  - rank_random: deterministic seeded permutation
  - _ks_for_level: k values per difficulty level
  - BM25 global variant (floored IDF from repobench_r_helix_global)
  - Full pipeline simulation: make_query + rank + acc_at against an inline fixture

All tests are pure-Python; no HuggingFace downloads, no Helix server, no CUDA.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

# Make benchmarks/ importable without packaging
BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

from repobench_r import (  # noqa: E402
    acc_at,
    tok,
    make_query,
    rank_overlap,
    rank_bm25,
    rank_random,
    _ks_for_level,
)
from repobench_r_helix_global import BM25  # noqa: E402


# ================================================================================
# acc_at -- the fundamental metric
# ================================================================================

class TestAccAt:
    def test_gold_at_position_0_acc1(self):
        assert acc_at([2, 0, 1], gold=2, k=1) == 1.0

    def test_gold_not_in_top1(self):
        assert acc_at([0, 1, 2], gold=2, k=1) == 0.0

    def test_gold_in_top3(self):
        assert acc_at([0, 1, 2], gold=2, k=3) == 1.0

    def test_gold_at_position_2_acc3_boundary(self):
        # index 2 is the third element (0-based) -- within top-3
        assert acc_at([0, 1, 2, 3, 4], gold=2, k=3) == 1.0

    def test_gold_at_position_3_not_in_top3(self):
        assert acc_at([0, 1, 2, 3, 4], gold=3, k=3) == 0.0

    def test_gold_in_top5(self):
        assert acc_at([0, 1, 2, 3, 4], gold=4, k=5) == 1.0

    def test_gold_not_in_top5(self):
        assert acc_at([0, 1, 2, 3, 4], gold=5, k=5) == 0.0

    def test_empty_order_miss(self):
        assert acc_at([], gold=0, k=1) == 0.0

    def test_k_larger_than_list(self):
        # k=10 but only 3 items; gold present -> hit
        assert acc_at([0, 1, 2], gold=1, k=10) == 1.0


# ================================================================================
# _ks_for_level
# ================================================================================

class TestKsForLevel:
    def test_easy_returns_1_3(self):
        assert _ks_for_level("easy") == [1, 3]

    def test_hard_returns_1_3_5(self):
        assert _ks_for_level("hard") == [1, 3, 5]

    def test_unknown_level_treated_as_easy(self):
        # Any non-"hard" level -> [1, 3]
        assert _ks_for_level("medium") == [1, 3]


# ================================================================================
# tok -- identifier tokenizer
# ================================================================================

class TestTok:
    def test_splits_on_non_identifier_chars(self):
        assert tok("foo.bar(baz)") == ["foo", "bar", "baz"]

    def test_ignores_leading_digits(self):
        # The regex finds identifier-shaped substrings anywhere in the string.
        # "123abc" contains the suffix "abc" starting after the digits, so tok
        # correctly extracts it -- the tokenizer is substring-based, not word-boundary.
        result = tok("123abc def")
        assert "def" in result
        assert "abc" in result   # correct: regex finds it as suffix of 123abc
        assert "123" not in result  # purely numeric tokens are never matched

    def test_underscore_prefix(self):
        assert "_private" in tok("_private = 1")

    def test_empty_string(self):
        assert tok("") == []

    def test_none_safe(self):
        assert tok(None) == []


# ================================================================================
# make_query
# ================================================================================

class TestMakeQuery:
    def _example(self, code, imports=""):
        return {"code": code, "import_statement": imports}

    def test_uses_last_30_lines(self):
        lines = [f"line_{i}" for i in range(50)]
        ex = self._example("\n".join(lines))
        q = make_query(ex)
        assert "line_49" in q
        assert "line_19" not in q  # only last 30 -> lines 20-49

    def test_prepends_imports(self):
        ex = self._example("x = 1", "import os")
        q = make_query(ex)
        assert q.startswith("import os")

    def test_empty_example(self):
        q = make_query({})
        assert isinstance(q, str)


# ================================================================================
# rank_overlap
# ================================================================================

class TestRankOverlap:
    def test_best_match_ranked_first(self):
        query = "def compute_fibonacci(n):"
        cands = [
            "def unrelated(): pass",
            "def compute_fibonacci(n): return n if n < 2 else ...",
            "import sys",
        ]
        order = rank_overlap(query, cands)
        assert order[0] == 1  # candidate 1 shares the most tokens

    def test_no_overlap_gives_stable_order(self):
        query = "zzz"
        cands = ["aaa", "bbb", "ccc"]
        order = rank_overlap(query, cands)
        assert sorted(order) == [0, 1, 2]  # all returned, no crash

    def test_empty_query_returns_all(self):
        order = rank_overlap("", ["a", "b", "c"])
        assert sorted(order) == [0, 1, 2]

    def test_returns_all_candidates(self):
        cands = ["a b", "c d", "e f", "g h", "i j"]
        order = rank_overlap("a b", cands)
        assert sorted(order) == list(range(len(cands)))


# ================================================================================
# rank_bm25
# ================================================================================

class TestRankBM25:
    def test_best_match_first(self):
        pytest.importorskip("rank_bm25")
        query = "fibonacci sequence recursive"
        cands = [
            "bubble sort algorithm implementation",
            "fibonacci sequence recursive function returns",
            "hash table collision resolution",
        ]
        order = rank_bm25(query, cands)
        assert order[0] == 1

    def test_returns_all_candidates(self):
        pytest.importorskip("rank_bm25")
        cands = ["alpha", "beta", "gamma"]
        order = rank_bm25("alpha", cands)
        assert sorted(order) == [0, 1, 2]

    def test_empty_corpus_safe(self):
        pytest.importorskip("rank_bm25")
        order = rank_bm25("query", ["", "", ""])
        assert sorted(order) == [0, 1, 2]


# ================================================================================
# rank_random
# ================================================================================

class TestRankRandom:
    def test_deterministic_same_key(self):
        cands = ["a", "b", "c", "d", "e"]
        o1 = rank_random(cands, ("easy", 42))
        o2 = rank_random(cands, ("easy", 42))
        assert o1 == o2

    def test_different_keys_give_different_orders(self):
        cands = list(range(20))
        o1 = rank_random(cands, ("easy", 0))
        o2 = rank_random(cands, ("easy", 1))
        # With 20 elements it would be astronomically unlikely to match.
        assert o1 != o2

    def test_returns_all_indices(self):
        cands = ["a", "b", "c"]
        order = rank_random(cands, "key")
        assert sorted(order) == [0, 1, 2]


# ================================================================================
# BM25 (global floored-IDF variant from repobench_r_helix_global)
# ================================================================================

class TestGlobalBM25:
    def _bm(self, corpus):
        return BM25([c.lower().split() for c in corpus])

    def test_idf_floored_at_zero(self):
        """Terms in every document should NOT go negative."""
        corpus = ["common token here", "common token there", "common token everywhere"]
        bm = self._bm(corpus)
        for idf_val in bm.idf.values():
            assert idf_val >= 0.0, f"negative IDF: {idf_val}"

    def test_relevant_doc_scores_higher(self):
        corpus = [
            "import os path join exists",
            "fibonacci recursive sequence memoize",
            "hash map collision linear probe",
        ]
        bm = self._bm(corpus)
        sc = bm.scores("fibonacci recursive".split())
        assert sc[1] > sc[0]
        assert sc[1] > sc[2]

    def test_zero_scores_for_no_overlap(self):
        corpus = ["apple banana", "cherry date"]
        bm = self._bm(corpus)
        sc = bm.scores(["zzz"])
        assert sc[0] == 0.0
        assert sc[1] == 0.0

    def test_returns_one_score_per_doc(self):
        corpus = ["a b c", "d e f", "g h i"]
        bm = self._bm(corpus)
        assert len(bm.scores(["a"])) == 3


# ================================================================================
# Full pipeline fixture -- end-to-end acc@k computation
# ================================================================================

# Inline fixture simulating one "easy" and one "hard" RepoBench-R example.
_FIXTURE_EXAMPLES = [
    {
        "level": "easy",
        "query": "import utils\ndef process(data):\n    result = utils.transform(data)",
        "candidates": [
            "def unrelated_function(): pass",             # idx 0
            "def transform(data): return data[::-1]",    # idx 1  <- GOLD
            "class Config:\n    debug = True",           # idx 2
        ],
        "gold": 1,
    },
    {
        "level": "hard",
        "query": "from parser import parse_tokens\ntokens = parse_tokens(src)",
        "candidates": [
            "def irrelevant(): return None",              # idx 0
            "class Config: pass",                         # idx 1
            "def parse_tokens(src): return src.split()", # idx 2  <- GOLD
            "import re",                                  # idx 3
            "def helper(x): return x + 1",               # idx 4
        ],
        "gold": 2,
    },
]


class TestFullPipelineFixture:
    """End-to-end: query construction -> overlap ranking -> acc@k scoring."""

    def _score_example(self, ex):
        ks = _ks_for_level(ex["level"])
        q = ex["query"]
        cands = ex["candidates"]
        gold = ex["gold"]
        order = rank_overlap(q, cands)
        return {k: acc_at(order, gold, k) for k in ks}

    def test_easy_example_overlap_finds_gold(self):
        scores = self._score_example(_FIXTURE_EXAMPLES[0])
        # "transform" appears in both query and candidate 1
        assert scores[1] == 1.0
        assert scores[3] == 1.0

    def test_hard_example_overlap_finds_gold(self):
        scores = self._score_example(_FIXTURE_EXAMPLES[1])
        # "parse_tokens" and "src" appear in both query and candidate 2
        assert scores[3] == 1.0
        assert scores[5] == 1.0  # acc@5 always >= acc@3

    def test_acc_monotonic(self):
        """acc@k must be non-decreasing in k."""
        for ex in _FIXTURE_EXAMPLES:
            ks = _ks_for_level(ex["level"])
            order = rank_overlap(ex["query"], ex["candidates"])
            vals = [acc_at(order, ex["gold"], k) for k in ks]
            for i in range(len(vals) - 1):
                assert vals[i] <= vals[i + 1], (
                    f"acc@k not monotonic for {ex['level']}: {list(zip(ks, vals))}"
                )

    def test_aggregate_accuracy(self):
        """Accumulate hits across both examples and verify the aggregate."""
        total = {"overlap": {}}
        counts = {}
        for ex in _FIXTURE_EXAMPLES:
            ks = _ks_for_level(ex["level"])
            order = rank_overlap(ex["query"], ex["candidates"])
            for k in ks:
                total["overlap"][k] = total["overlap"].get(k, 0.0) + acc_at(order, ex["gold"], k)
                counts[k] = counts.get(k, 0) + 1
        # acc@3 is a hit in both examples (easy and hard)
        assert total["overlap"].get(3, 0.0) == 2.0

    def test_wrong_gold_gives_zero(self):
        """Sanity: if gold is beyond the candidate list, acc_at returns 0."""
        order = [0, 1, 2]
        assert acc_at(order, gold=99, k=3) == 0.0

    def test_hard_level_has_acc5_key(self):
        """Hard examples must produce an acc@5 value (not just acc@1/3)."""
        scores = self._score_example(_FIXTURE_EXAMPLES[1])
        assert 5 in scores, f"acc@5 missing from hard example scores: {scores}"
