"""Rank-sensitive retrieval metric for the claude-matrix bench (issue #137).

``gold_delivered`` is a boolean -- it cannot see dense recall reshuffling
*which* rank a gold document lands at when the gold doc was already in
the delivered set. ``gold_match_rank`` is the rank-aware generalization:
the 1-based position of the first delivered source matching the needle's
``gold_source`` list, or ``None`` when no gold doc was delivered.

    gold_match_rank(delivered, gold) is not None   ==   the old gold_delivered

These tests exercise the real ``bench_claude_matrix`` functions directly
-- no duplicated predicate, which is the anti-pattern issue #137 removes.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make benchmarks/ importable without packaging it (matches the
# convention used by tests/test_bench_needles.py).
BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

from bench_claude_matrix import gold_match_rank, summarize_profile  # noqa: E402


# ─── gold_match_rank: position of the first gold-matching delivery ────


def test_rank_is_one_when_gold_is_first_delivered():
    rank = gold_match_rank(
        ["F:/Projects/helix-context/helix.toml"],
        ["helix-context/helix.toml"],
    )
    assert rank == 1


def test_rank_reflects_later_position():
    delivered = [
        "F:/Projects/other/a.md",
        "F:/Projects/other/b.md",
        "F:/Projects/helix-context/helix.toml",
    ]
    assert gold_match_rank(delivered, ["helix-context/helix.toml"]) == 3


def test_rank_is_none_when_no_delivered_source_matches():
    delivered = ["F:/Projects/Education/CLAUDE.md"]
    assert gold_match_rank(delivered, ["helix-context/helix.toml"]) is None


def test_rank_returns_earliest_when_multiple_sources_match():
    """Two delivered docs satisfy the gold list -- rank is the earliest."""
    delivered = [
        "F:/Projects/other/a.md",
        "F:/Projects/helix-context/README.md",
        "F:/Projects/helix-context/helix.toml",
    ]
    gold = ["helix-context/helix.toml", "helix-context/README.md"]
    assert gold_match_rank(delivered, gold) == 2


def test_rank_multi_valid_gold_matches_any_entry():
    """A later gold_source entry still produces the delivered position."""
    delivered = [
        "F:/Projects/other/a.md",
        "F:/Projects/helix-context/docs/SETUP.md",
    ]
    gold = ["helix-context/helix.toml", "helix-context/docs/SETUP.md"]
    assert gold_match_rank(delivered, gold) == 2


def test_rank_is_case_insensitive():
    rank = gold_match_rank(
        ["F:/Projects/Helix-Context/HELIX.TOML"],
        ["helix-context/helix.toml"],
    )
    assert rank == 1


def test_rank_normalizes_backslashes():
    rank = gold_match_rank(
        ["F:\\Projects\\helix-context\\helix.toml"],
        ["helix-context/helix.toml"],
    )
    assert rank == 1


def test_rank_ignores_empty_gold_entries():
    """An empty gold_source string must not match every delivered doc.

    Bare substring containment treats '' as 'in everything'; the rank
    function skips empty/whitespace gold entries so a malformed needle
    cannot silently report rank 1 on every query.
    """
    delivered = ["F:/Projects/Education/CLAUDE.md"]
    assert gold_match_rank(delivered, ["", "   "]) is None


def test_rank_none_is_equivalent_to_legacy_gold_not_delivered():
    """(gold_match_rank is not None) reproduces the old gold_delivered bool."""
    gold = ["helix-context/helix.toml"]
    hit = ["F:/Projects/helix-context/helix.toml"]
    miss = ["F:/Projects/Education/CLAUDE.md"]
    assert (gold_match_rank(hit, gold) is not None) is True
    assert (gold_match_rank(miss, gold) is not None) is False


# ─── summarize_profile: MRR over the profile's needles ────────────────


def _needle_record(score: int = 1, gold_rank: int | None = None,
                    cost: float = 0.0) -> dict:
    """Minimal per-needle record shaped like run_one_needle's output."""
    return {
        "score": score,
        "retrieval": {
            "gold_delivered": gold_rank is not None,
            "gold_rank": gold_rank,
        },
        "cost_usd": cost,
    }


def test_summarize_profile_reports_mrr():
    """MRR is the mean reciprocal gold rank over every needle in the run."""
    per_needle = [
        _needle_record(gold_rank=1),     # rr 1.0
        _needle_record(gold_rank=2),     # rr 0.5
        _needle_record(gold_rank=None),  # rr 0.0 (gold missed)
    ]
    summary = summarize_profile(per_needle, n_needles=3)
    assert summary["retrieval"]["mrr"] == 0.5


def test_summarize_profile_mrr_counts_a_miss_as_zero():
    per_needle = [
        _needle_record(gold_rank=1),     # rr 1.0
        _needle_record(gold_rank=None),  # rr 0.0
    ]
    summary = summarize_profile(per_needle, n_needles=2)
    assert summary["retrieval"]["mrr"] == 0.5


def test_mrr_separates_runs_with_identical_gold_delivered_rate():
    """The reason issue #137 exists.

    Two runs deliver the gold doc for every needle -- identical
    gold_delivered_count and gold_delivered_rate -- but one lands it at
    rank 1 and the other at rank 5. The boolean metric cannot tell them
    apart; MRR must.
    """
    run_top = [_needle_record(gold_rank=1) for _ in range(3)]
    run_deep = [_needle_record(gold_rank=5) for _ in range(3)]

    top = summarize_profile(run_top, n_needles=3)["retrieval"]
    deep = summarize_profile(run_deep, n_needles=3)["retrieval"]

    # The coarse metric is blind to the difference ...
    assert top["gold_delivered_rate"] == deep["gold_delivered_rate"] == 1.0
    # ... the rank-sensitive metric is not.
    assert top["mrr"] == 1.0
    assert deep["mrr"] == 0.2
    assert top["mrr"] > deep["mrr"]


def test_summarize_profile_keeps_legacy_gold_delivered_fields():
    """gold_delivered_count / _rate stay intact so old JSONL stays comparable."""
    per_needle = [
        _needle_record(gold_rank=1),
        _needle_record(gold_rank=None),
        _needle_record(gold_rank=3),
    ]
    retr = summarize_profile(per_needle, n_needles=3)["retrieval"]
    assert retr["gold_delivered_count"] == 2
    assert retr["gold_delivered_rate"] == 2 / 3
