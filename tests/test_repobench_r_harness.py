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
    """acc_at: the core scoring function, across gold position / k boundaries."""

    @pytest.mark.parametrize(
        ("order", "gold", "k", "expected"),
        [
            pytest.param([2, 0, 1], 2, 1, 1.0, id="gold-at-position-0-acc1"),
            pytest.param([0, 1, 2], 2, 1, 0.0, id="gold-not-in-top1"),
            pytest.param([0, 1, 2], 2, 3, 1.0, id="gold-in-top3"),
            # index 2 is the third element (0-based) -- within top-3
            pytest.param([0, 1, 2, 3, 4], 2, 3, 1.0, id="gold-at-position-2-acc3-boundary"),
            pytest.param([0, 1, 2, 3, 4], 3, 3, 0.0, id="gold-at-position-3-not-in-top3"),
            pytest.param([0, 1, 2, 3, 4], 4, 5, 1.0, id="gold-in-top5"),
            pytest.param([0, 1, 2, 3, 4], 5, 5, 0.0, id="gold-not-in-top5"),
            pytest.param([], 0, 1, 0.0, id="empty-order-miss"),
            # k=10 but only 3 items; gold present -> hit
            pytest.param([0, 1, 2], 1, 10, 1.0, id="k-larger-than-list"),
        ],
    )
    def test_acc_at(self, order, gold, k, expected):
        assert acc_at(order, gold=gold, k=k) == expected


# ================================================================================
# _ks_for_level
# ================================================================================

class TestKsForLevel:
    """_ks_for_level: k values per difficulty level."""

    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            pytest.param("easy", [1, 3], id="easy-returns-1-3"),
            pytest.param("hard", [1, 3, 5], id="hard-returns-1-3-5"),
            # Any non-"hard" level -> [1, 3]
            pytest.param("medium", [1, 3], id="unknown-level-treated-as-easy"),
        ],
    )
    def test_ks_for_level(self, level, expected):
        assert _ks_for_level(level) == expected


# ================================================================================
# tok -- identifier tokenizer
# ================================================================================

class TestTok:
    """tok: identifier tokenizer."""

    @pytest.mark.parametrize(
        ("s", "expected"),
        [
            pytest.param("foo.bar(baz)", ["foo", "bar", "baz"], id="splits-on-non-identifier-chars"),
            # The regex finds identifier-shaped substrings anywhere in the string.
            # "123abc" contains the suffix "abc" starting after the digits, so tok
            # correctly extracts it -- the tokenizer is substring-based, not
            # word-boundary. Purely numeric tokens ("123") are never matched.
            pytest.param("123abc def", ["abc", "def"], id="ignores-leading-digits-substring-match"),
            pytest.param("_private = 1", ["_private"], id="underscore-prefix"),
            pytest.param("", [], id="empty-string"),
            pytest.param(None, [], id="none-safe"),
        ],
    )
    def test_tok(self, s, expected):
        assert tok(s) == expected


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
    """rank_random: deterministic seeded permutation."""

    @pytest.mark.parametrize(
        ("cands", "key1", "key2", "expect_equal"),
        [
            pytest.param(
                ["a", "b", "c", "d", "e"], ("easy", 42), ("easy", 42), True,
                id="deterministic-same-key",
            ),
            # With 20 elements it would be astronomically unlikely to match.
            pytest.param(
                list(range(20)), ("easy", 0), ("easy", 1), False,
                id="different-keys-give-different-orders",
            ),
        ],
    )
    def test_rank_random_key_sensitivity(self, cands, key1, key2, expect_equal):
        o1 = rank_random(cands, key1)
        o2 = rank_random(cands, key2)
        assert (o1 == o2) is expect_equal

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
