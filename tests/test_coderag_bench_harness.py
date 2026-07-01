"""Unit tests for the CodeRAG-Bench Step-2 harness logic.

Covers:
  - ndcg_at, recall_at, precision_at: metric correctness + boundary conditions
  - BM25: floored IDF, score ordering, correct gold-rank
  - tok: identifier tokenizer
  - token_estimate / efficiency_stats / _percentile: efficiency layer
  - parse_doc_idx: source-id parser
  - preview_token_estimate: injected-token heuristic
  - run(): full scoring pipeline over an inline fixture (no HF, no server, no GPU)
  - score_queries() mocked: confirms NDCG/Recall/Precision accumulation with a
    fake /fingerprint response

All tests are pure-Python; zero network, zero Helix server, zero GPU.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make benchmarks/ importable without packaging.
BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

from coderag_bench import (  # noqa: E402
    BM25,
    KS,
    _percentile,
    efficiency_stats,
    ndcg_at,
    precision_at,
    recall_at,
    run,
    tok,
    token_estimate,
)
from coderag_bench_helix import (  # noqa: E402
    parse_doc_idx,
    preview_token_estimate,
    score_queries,
)


# ===========================================================================
# ndcg_at
# ===========================================================================

class TestNdcgAt:
    def test_rank_1_ndcg10_is_one(self):
        """Gold at rank 1 (pos0=0) -> NDCG = 1/log2(2) = 1.0."""
        assert math.isclose(ndcg_at(0, 10), 1.0)

    def test_rank_2(self):
        expected = 1.0 / math.log2(3)  # pos0=1 -> rank=2 -> 1/log2(3)
        assert math.isclose(ndcg_at(1, 10), expected)

    def test_rank_10_boundary(self):
        """pos0=9 is rank 10 -- just inside @10."""
        val = ndcg_at(9, 10)
        assert val > 0.0
        assert math.isclose(val, 1.0 / math.log2(11))

    def test_rank_11_outside_k10(self):
        """pos0=10 is rank 11 -- outside @10."""
        assert ndcg_at(10, 10) == 0.0

    def test_very_low_rank_is_zero(self):
        assert ndcg_at(999, 10) == 0.0

    def test_ndcg_decreases_with_rank(self):
        vals = [ndcg_at(i, 10) for i in range(10)]
        for i in range(9):
            assert vals[i] > vals[i + 1]


# ===========================================================================
# recall_at
# ===========================================================================

class TestRecallAt:
    def test_hit_at_k(self):
        assert recall_at(0, 1) == 1.0
        assert recall_at(4, 5) == 1.0
        assert recall_at(9, 10) == 1.0

    def test_miss_at_k(self):
        assert recall_at(1, 1) == 0.0
        assert recall_at(5, 5) == 0.0
        assert recall_at(10, 10) == 0.0

    def test_all_ks_on_rank_1(self):
        for k in KS:
            assert recall_at(0, k) == 1.0

    def test_rank_beyond_ks(self):
        for k in KS:
            assert recall_at(999, k) == 0.0


# ===========================================================================
# precision_at
# ===========================================================================

class TestPrecisionAt:
    def test_hit_is_one_over_k(self):
        assert math.isclose(precision_at(0, 1), 1.0)
        assert math.isclose(precision_at(0, 5), 0.2)
        assert math.isclose(precision_at(0, 10), 0.1)

    def test_miss_is_zero(self):
        assert precision_at(5, 5) == 0.0
        assert precision_at(10, 10) == 0.0

    def test_gold_at_last_position_in_k(self):
        # pos0=k-1 is rank k -> hit
        assert math.isclose(precision_at(9, 10), 0.1)

    def test_gold_just_outside_k(self):
        assert precision_at(10, 10) == 0.0


# ===========================================================================
# tok (identifier tokenizer)
# ===========================================================================

class TestTok:
    def test_basic_identifier(self):
        assert "fibonacci" in tok("def fibonacci(n):")

    def test_lowercase(self):
        result = tok("HasCloseElements")
        assert all(t == t.lower() for t in result)

    def test_underscore_prefix(self):
        assert "_private" in tok("_private = 1")

    def test_empty_string(self):
        assert tok("") == []

    def test_none_safe(self):
        assert tok(None) == []

    def test_strips_punctuation(self):
        result = tok("foo.bar(baz)")
        assert "foo" in result
        assert "bar" in result
        assert "baz" in result


# ===========================================================================
# BM25
# ===========================================================================

class TestBM25:
    def _bm(self, corpus: list[str]) -> BM25:
        return BM25([tok(c) for c in corpus])

    def test_idf_non_negative(self):
        corpus = ["common term x", "common term y", "common term z"]
        bm = self._bm(corpus)
        for v in bm.idf.values():
            assert v >= 0.0, f"negative IDF: {v}"

    def test_relevant_doc_scores_higher(self):
        corpus = [
            "def has_close_elements numbers threshold",
            "import os path join exists",
            "class Config debug true",
        ]
        bm = self._bm(corpus)
        sc = bm.scores(tok("has_close_elements threshold"))
        assert sc[0] > sc[1]
        assert sc[0] > sc[2]

    def test_zero_score_for_no_overlap(self):
        corpus = ["apple banana", "cherry date"]
        bm = self._bm(corpus)
        sc = bm.scores(tok("zzz"))
        assert sc[0] == 0.0
        assert sc[1] == 0.0

    def test_rank_gold_pos_exact(self):
        """Gold doc contains all query tokens -> should rank first (pos=0)."""
        corpus = [
            "unrelated irrelevant filler text",
            "fibonacci sequence recursive memoize function",
            "hash map collision resolution",
        ]
        bm = self._bm(corpus)
        pos = bm.rank_gold_pos(tok("fibonacci recursive"), gold_idx=1)
        assert pos == 0

    def test_rank_gold_pos_worst_case(self):
        """Gold doc has zero query token overlap -> ranks last."""
        corpus = [
            "fibonacci sequence recursive",
            "apple banana cherry",  # gold -- no overlap with query
        ]
        bm = self._bm(corpus)
        pos = bm.rank_gold_pos(tok("fibonacci recursive"), gold_idx=1)
        assert pos == 1  # index 1 = last in a 2-doc corpus

    def test_scores_length_matches_corpus(self):
        corpus = ["a b c", "d e f", "g h i"]
        bm = self._bm(corpus)
        assert len(bm.scores(tok("a"))) == 3

    def test_empty_corpus_graceful(self):
        bm = BM25([])
        assert bm.scores([]) == []


# ===========================================================================
# token_estimate + efficiency layer
# ===========================================================================

class TestTokenEstimate:
    def test_single_doc_one_word(self):
        # 1 word * 1.3 = 1.3 -> rounded 1
        assert token_estimate(["hello"]) == 1

    def test_proportional_to_words(self):
        # 10 words -> round(10 * 1.3) = 13
        text = " ".join(["word"] * 10)
        assert token_estimate([text]) == 13

    def test_empty(self):
        assert token_estimate([]) == 0

    def test_multiple_docs_sum(self):
        docs = ["a b c", "d e"]  # 5 words -> round(5*1.3) = 7 (actually 6.5->7)
        est = token_estimate(docs)
        assert est == round(5 * 1.3)


class TestPercentile:
    def test_median_of_odd(self):
        assert _percentile([1, 2, 3, 4, 5], 50) == 3.0

    def test_p90_of_ten(self):
        vals = list(range(1, 11))  # 1..10
        # p90 of [1..10] = 9.1 (linear interp)
        result = _percentile(vals, 90)
        assert 9.0 <= result <= 10.0

    def test_empty(self):
        assert _percentile([], 50) == 0.0

    def test_single(self):
        assert _percentile([42.0], 50) == 42.0
        assert _percentile([42.0], 90) == 42.0


class TestEfficiencyStats:
    def test_keys_present(self):
        stats = efficiency_stats([100.0, 200.0, 300.0])
        assert "median_injected_tokens" in stats
        assert "p90_injected_tokens" in stats

    def test_median_correct(self):
        stats = efficiency_stats([100.0, 200.0, 300.0])
        assert math.isclose(stats["median_injected_tokens"], 200.0, abs_tol=1.0)

    def test_empty(self):
        stats = efficiency_stats([])
        assert stats["median_injected_tokens"] == 0.0


# ===========================================================================
# parse_doc_idx
# ===========================================================================

class TestParseDocIdx:
    def test_normal(self):
        assert parse_doc_idx("doc_42") == 42

    def test_zero(self):
        assert parse_doc_idx("doc_0") == 0

    def test_large(self):
        assert parse_doc_idx("doc_9999") == 9999

    def test_none(self):
        assert parse_doc_idx(None) is None

    def test_empty(self):
        assert parse_doc_idx("") is None

    def test_wrong_prefix(self):
        assert parse_doc_idx("cand_3") is None

    def test_partial_match_rejected(self):
        assert parse_doc_idx("doc_42_extra") is None

    def test_non_numeric(self):
        assert parse_doc_idx("doc_abc") is None


# ===========================================================================
# preview_token_estimate
# ===========================================================================

class TestPreviewTokenEstimate:
    def test_empty(self):
        assert preview_token_estimate([]) == 0

    def test_single_word(self):
        assert preview_token_estimate(["hello"]) == 1

    def test_proportional(self):
        previews = ["a b c d e"] * 2  # 10 words -> round(13) = 13
        assert preview_token_estimate(previews) == 13

    def test_none_safe(self):
        assert preview_token_estimate([None, None]) == 0  # type: ignore[list-item]


# ===========================================================================
# Full pipeline: run() over an inline fixture
# ===========================================================================

# Inline fixture: 10 docs, 4 queries (2 humaneval, 2 mbpp).
_CORPUS = [
    {"doc_id": "humaneval:0", "text": "def has_close_elements(numbers, threshold):\n    for i, a in enumerate(numbers):\n        for j, b in enumerate(numbers):\n            if i != j and abs(a-b) < threshold:\n                return True\n    return False\n"},  # noqa: E501
    {"doc_id": "humaneval:1", "text": "def separate_paren_groups(paren_string):\n    result = []\n    current = []\n    depth = 0\n    for c in paren_string:\n        if c == '(': depth += 1; current.append(c)\n        elif c == ')': depth -= 1; current.append(c)\n        if depth == 0 and current: result.append(''.join(current)); current = []\n    return result\n"},  # noqa: E501
    {"doc_id": "humaneval:2", "text": "def truncate_number(number):\n    return number % 1.0\n"},
    {"doc_id": "mbpp:1", "text": "def sum_list(lst):\n    return sum(lst)\n"},
    {"doc_id": "mbpp:2", "text": "def find_max(lst):\n    return max(lst)\n"},
    {"doc_id": "mbpp:3", "text": "def reverse_string(s):\n    return s[::-1]\n"},
    {"doc_id": "mbpp:4", "text": "def count_vowels(s):\n    return sum(1 for c in s if c in 'aeiou')\n"},
    {"doc_id": "mbpp:5", "text": "def is_palindrome(s):\n    return s == s[::-1]\n"},
    {"doc_id": "mbpp:6", "text": "def flatten(lst):\n    return [x for sub in lst for x in sub]\n"},
    {"doc_id": "mbpp:7", "text": "def factorial(n):\n    return 1 if n <= 1 else n * factorial(n-1)\n"},
]
_DOC_INDEX = {c["doc_id"]: i for i, c in enumerate(_CORPUS)}

_QUERIES = [
    {
        "ds": "humaneval",
        "query": "def has_close_elements(numbers, threshold):\n    \"\"\"Check if any two numbers are closer than threshold.\"\"\"\n",
        "gold": "humaneval:0",
    },
    {
        "ds": "humaneval",
        "query": "def separate_paren_groups(paren_string):\n    \"\"\"Input: string of nested parentheses groups.\"\"\"\n",
        "gold": "humaneval:1",
    },
    {
        "ds": "mbpp",
        "query": "Write a function to find the factorial of a number.",
        "gold": "mbpp:7",
    },
    {
        "ds": "mbpp",
        "query": "Write a function to check if a string is a palindrome.",
        "gold": "mbpp:5",
    },
]


class TestRunPipeline:
    def test_run_returns_summary_and_rows(self):
        summary, rows = run(_CORPUS, _DOC_INDEX, _QUERIES, limit=0)
        assert "humaneval" in summary
        assert "mbpp" in summary
        assert len(rows) == len(_QUERIES)

    def test_ndcg_in_0_to_1(self):
        summary, _ = run(_CORPUS, _DOC_INDEX, _QUERIES)
        for ds, s in summary.items():
            assert 0.0 <= s["bm25_ndcg@10"] <= 1.0, f"{ds}: ndcg out of range"
            assert 0.0 <= s["rand_ndcg@10"] <= 1.0, f"{ds}: rand ndcg out of range"

    def test_recall_in_0_to_1(self):
        summary, _ = run(_CORPUS, _DOC_INDEX, _QUERIES)
        for ds, s in summary.items():
            for k in KS:
                assert 0.0 <= s[f"bm25_recall@{k}"] <= 1.0
                assert 0.0 <= s[f"rand_recall@{k}"] <= 1.0

    def test_precision_in_0_to_1(self):
        summary, _ = run(_CORPUS, _DOC_INDEX, _QUERIES)
        for ds, s in summary.items():
            for k in KS:
                assert 0.0 <= s[f"bm25_precision@{k}"] <= 1.0

    def test_recall_monotonic(self):
        """recall@1 <= recall@5 <= recall@10 for BM25 arm."""
        summary, _ = run(_CORPUS, _DOC_INDEX, _QUERIES)
        for ds, s in summary.items():
            r1 = s["bm25_recall@1"]
            r5 = s["bm25_recall@5"]
            r10 = s["bm25_recall@10"]
            assert r1 <= r5 <= r10, f"{ds}: recall not monotonic: {r1}, {r5}, {r10}"

    def test_bm25_lexical_hit_humaneval(self):
        """HumanEval gold docs share function names with queries -> BM25 should
        achieve high recall@10 on this tiny fixture."""
        summary, _ = run(_CORPUS, _DOC_INDEX, _QUERIES)
        # The humaneval queries contain exact function names that appear in the
        # gold docs. BM25 should rank them in top-10 of 10 docs.
        assert summary["humaneval"]["bm25_recall@10"] == 1.0, (
            f"Expected BM25 recall@10=1.0 on lexically-easy humaneval fixture, "
            f"got {summary['humaneval']['bm25_recall@10']}"
        )

    def test_efficiency_keys_present(self):
        summary, _ = run(_CORPUS, _DOC_INDEX, _QUERIES)
        for ds, s in summary.items():
            eff = s.get("bm25_efficiency", {})
            assert "median_injected_tokens" in eff, f"{ds}: missing median_injected_tokens"
            assert "p90_injected_tokens" in eff, f"{ds}: missing p90_injected_tokens"

    def test_corpus_size_in_summary(self):
        summary, _ = run(_CORPUS, _DOC_INDEX, _QUERIES)
        for ds, s in summary.items():
            assert s["corpus"] == len(_CORPUS)

    def test_n_per_ds_correct(self):
        summary, _ = run(_CORPUS, _DOC_INDEX, _QUERIES)
        assert summary["humaneval"]["n"] == 2
        assert summary["mbpp"]["n"] == 2

    def test_limit_caps_queries(self):
        summary, rows = run(_CORPUS, _DOC_INDEX, _QUERIES, limit=1)
        # With limit=1, at most 1 query per dataset.
        for ds, s in summary.items():
            assert s["n"] <= 1
        # Total rows capped to at most len(datasets) * 1 = 2.
        assert len(rows) <= 2

    def test_unresolvable_gold_dropped(self):
        """Queries with gold not in doc_index should be silently dropped."""
        bad_queries = [
            {"ds": "humaneval", "query": "foo bar", "gold": "humaneval:999"},
        ]
        summary, rows = run(_CORPUS, _DOC_INDEX, bad_queries)
        # No rows if all queries are unresolvable.
        assert len(rows) == 0
        assert len(summary) == 0

    def test_per_query_rows_have_gold_idx(self):
        _, rows = run(_CORPUS, _DOC_INDEX, _QUERIES)
        for r in rows:
            assert "gold_idx" in r, "per-query row missing gold_idx"

    def test_bm25_ndcg_beats_random_on_lexical_corpus(self):
        """BM25 should beat random on a lexically saturated corpus."""
        summary, _ = run(_CORPUS, _DOC_INDEX, _QUERIES)
        for ds in ("humaneval",):
            bm_ndcg = summary[ds]["bm25_ndcg@10"]
            rand_ndcg = summary[ds]["rand_ndcg@10"]
            assert bm_ndcg >= rand_ndcg, (
                f"{ds}: BM25 ndcg@10={bm_ndcg} should be >= random ndcg@10={rand_ndcg}"
            )


# ===========================================================================
# score_queries() with mocked /fingerprint calls
# ===========================================================================

class TestScoreQueriesMocked:
    """Test the Helix arm scoring path using a fake fingerprint() function."""

    # A tiny query set with gold resolved.
    _QUERIES = [
        {"ds": "humaneval", "query": "def has_close_elements", "gold": "humaneval:0", "gold_idx": 0},
        {"ds": "humaneval", "query": "def separate_paren_groups", "gold": "humaneval:1", "gold_idx": 1},
        {"ds": "mbpp", "query": "factorial of a number", "gold": "mbpp:7", "gold_idx": 9},
    ]

    def _make_fps(self, ranked_idxs: list[int]):
        """Build a fake fingerprints list (the inner list, not the full tuple)."""
        return [
            {"rank": r, "source": "doc_{}".format(idx), "score": 1.0 / (r + 1),
             "preview": "def func_{} pass ".format(idx) * 5}
            for r, idx in enumerate(ranked_idxs)
        ]

    def test_ndcg_perfect_ranking(self):
        """If gold is always ranked #1 (pos0=0), ndcg@10 = 1.0 for all."""
        queries = self._QUERIES

        def fake_fingerprint(url, query, max_results, timeout_s):
            # Find which query this is by matching query text.
            for q in queries:
                if q["query"] in query:
                    return self._make_fps(
                        [q["gold_idx"]] + [99, 88, 77, 66, 55, 44, 33, 22, 11]
                    ), 5.0
            return [], 5.0

        with patch("coderag_bench_helix.fingerprint", side_effect=fake_fingerprint):
            summary, rows = score_queries(
                queries=queries,
                helix_url="http://mock",
                max_results=10,
            )

        for ds, s in summary.items():
            assert math.isclose(s["helix_ndcg@10"], 1.0, abs_tol=1e-6), (
                f"{ds}: expected perfect ndcg@10, got {s['helix_ndcg@10']}"
            )

    def test_ndcg_zero_when_gold_not_retrieved(self):
        """If gold never appears in fingerprints, ndcg@10 = 0.0."""
        def fake_fingerprint(url, query, max_results, timeout_s):
            # Return docs with indices that never include the gold.
            return self._make_fps([99, 88, 77, 66, 55]), 5.0

        with patch("coderag_bench_helix.fingerprint", side_effect=fake_fingerprint):
            summary, rows = score_queries(
                queries=self._QUERIES[:1],  # just humaneval:0 (gold_idx=0)
                helix_url="http://mock",
                max_results=10,
                n_corpus=100,
            )

        assert summary["humaneval"]["helix_ndcg@10"] == 0.0

    def test_recall_at_1_correct(self):
        """Gold ranked first -> recall@1 = 1.0."""
        q = [self._QUERIES[0]]  # humaneval:0, gold_idx=0

        def fake_fingerprint(url, query, max_results, timeout_s):
            return self._make_fps([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]), 5.0

        with patch("coderag_bench_helix.fingerprint", side_effect=fake_fingerprint):
            summary, _ = score_queries(q, helix_url="http://mock", max_results=10)

        assert summary["humaneval"]["helix_recall@1"] == 1.0

    def test_recall_at_5_but_not_1(self):
        """Gold ranked 3rd -> recall@1=0, recall@5=1."""
        q = [self._QUERIES[0]]  # gold_idx=0

        def fake_fingerprint(url, query, max_results, timeout_s):
            # Gold (idx=0) is ranked 3rd (pos=2).
            return self._make_fps([1, 2, 0, 3, 4, 5, 6, 7, 8, 9]), 5.0

        with patch("coderag_bench_helix.fingerprint", side_effect=fake_fingerprint):
            summary, _ = score_queries(q, helix_url="http://mock", max_results=10)

        assert summary["humaneval"]["helix_recall@1"] == 0.0
        assert summary["humaneval"]["helix_recall@5"] == 1.0

    def test_precision_at_k_formula(self):
        """Gold ranked first -> precision@k = 1/k."""
        q = [self._QUERIES[0]]  # gold_idx=0

        def fake_fingerprint(url, query, max_results, timeout_s):
            return self._make_fps([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]), 5.0

        with patch("coderag_bench_helix.fingerprint", side_effect=fake_fingerprint):
            summary, _ = score_queries(q, helix_url="http://mock", max_results=10)

        s = summary["humaneval"]
        assert math.isclose(s["helix_precision@1"], 1.0)
        assert math.isclose(s["helix_precision@5"], 0.2, abs_tol=1e-6)
        assert math.isclose(s["helix_precision@10"], 0.1, abs_tol=1e-6)

    def test_efficiency_keys_present(self):
        q = [self._QUERIES[0]]

        def fake_fingerprint(url, query, max_results, timeout_s):
            return self._make_fps([0, 1, 2]), 12.5

        with patch("coderag_bench_helix.fingerprint", side_effect=fake_fingerprint):
            summary, _ = score_queries(q, helix_url="http://mock", max_results=10)

        eff = summary["humaneval"]["efficiency"]
        assert "median_injected_tokens" in eff
        assert "p90_injected_tokens" in eff
        assert "median_latency_ms" in eff
        assert "p90_latency_ms" in eff

    def test_latency_recorded(self):
        q = [self._QUERIES[0]]

        def fake_fingerprint(url, query, max_results, timeout_s):
            return self._make_fps([0, 1, 2]), 42.0

        with patch("coderag_bench_helix.fingerprint", side_effect=fake_fingerprint):
            summary, rows = score_queries(q, helix_url="http://mock", max_results=10)

        assert rows[0]["latency_ms"] == 42.0
        assert summary["humaneval"]["efficiency"]["median_latency_ms"] == 42.0

    def test_network_error_counted_as_err(self):
        """A network failure should increment err, not crash the loop."""
        import urllib.error
        q = [self._QUERIES[0]]

        def fake_fingerprint(url, query, max_results, timeout_s):
            raise urllib.error.URLError("connection refused")

        with patch("coderag_bench_helix.fingerprint", side_effect=fake_fingerprint):
            summary, rows = score_queries(
                q, helix_url="http://mock", max_results=10, n_corpus=100
            )

        # Summary may be empty (n=0) since no successful queries.
        assert len(rows) == 1
        assert "error" in rows[0]

    def test_multi_ds_aggregated_separately(self):
        """Queries from different datasets must be aggregated independently."""
        queries = [
            self._QUERIES[0],  # humaneval
            self._QUERIES[2],  # mbpp
        ]

        def fake_fingerprint(url, query, max_results, timeout_s):
            for q in queries:
                if q["query"] in query:
                    return self._make_fps([q["gold_idx"]] + list(range(99, 89, -1))), 5.0
            return [], 5.0

        with patch("coderag_bench_helix.fingerprint", side_effect=fake_fingerprint):
            summary, _ = score_queries(queries, helix_url="http://mock", max_results=10)

        assert "humaneval" in summary
        assert "mbpp" in summary
        assert summary["humaneval"]["n"] == 1
        assert summary["mbpp"]["n"] == 1
