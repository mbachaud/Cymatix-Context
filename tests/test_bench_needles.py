"""Regression tests for the multi-valid-gold needle hit detection.

The 10-needle bench (``benchmarks/bench_claude_matrix.py`` + the legacy
``benchmarks/bench_needle.py``) now labels each needle with a list of
valid source files. A needle counts as gold-delivered when ANY entry in
``gold_source`` matches a citation source (case-insensitive,
forward-slash normalized substring match).

The single-source variant must still work: a 1-item list behaves
identically to the pre-multi-gold schema.

See docs/benchmarks/MULTI_VALID_GOLD.md for the curation rationale.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make benchmarks/ importable without packaging it (matches the
# convention used by tests/test_bench_citations.py).
BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

from bench_claude_matrix import (  # noqa: E402
    NEEDLES as MATRIX_NEEDLES,
    gold_match_rank,
)
from bench_needle import NEEDLES as NEEDLE_NEEDLES  # noqa: E402


# ─── The gold-match predicate ─────────────────────────────────────────
#
# bench_claude_matrix exposes the predicate as gold_match_rank (issue
# #137); "was the gold doc delivered at all" is exactly
# ``gold_match_rank(...) is not None``. These tests exercise that
# production function directly rather than a hand-copied predicate.


def _gold_delivered(delivered_sources, gold_sources) -> bool:
    """True iff any delivered source matches any gold source.

    Thin boolean adapter over bench_claude_matrix.gold_match_rank, so the
    pre-existing multi-valid-gold regression cases keep their original
    assertions while exercising the production predicate.
    """
    return gold_match_rank(delivered_sources, gold_sources) is not None


# ─── Multi-valid-gold ANY-match behavior ──────────────────────────────


def test_multi_gold_hit_when_first_source_appears():
    """A 2-valid-gold needle hits when the FIRST gold source is delivered."""
    gold = ["helix-context/helix.toml", "helix-context/docs/SETUP.md"]
    delivered = ["F:/Projects/helix-context/helix.toml"]
    assert _gold_delivered(delivered, gold) is True


def test_multi_gold_hit_when_second_source_appears():
    """A 2-valid-gold needle hits when the SECOND gold source is delivered.

    This is the load-bearing case: under strict single-gold the same
    delivery would miss, even though the answer is in a documentation
    file an honest reader would consider a valid source.
    """
    gold = ["helix-context/helix.toml", "helix-context/docs/SETUP.md"]
    delivered = ["F:/Projects/helix-context/docs/SETUP.md"]
    assert _gold_delivered(delivered, gold) is True


def test_multi_gold_miss_when_neither_source_appears():
    """A 2-valid-gold needle misses when NEITHER gold source is delivered."""
    gold = ["helix-context/helix.toml", "helix-context/docs/SETUP.md"]
    delivered = [
        "F:/Projects/Education/CLAUDE.md",
        "F:/Projects/BookKeeper/CLAUDE.md",
    ]
    assert _gold_delivered(delivered, gold) is False


def test_multi_gold_hit_with_multiple_candidates():
    """Realistic case: a 6-valid-gold needle hits on a middle-of-list entry."""
    gold = [
        "helix-context/helix.toml",
        "helix-context/README.md",
        "helix-context/CLAUDE.md",
        "helix-context/docs/SETUP.md",
        "helix-context/docs/TROUBLESHOOTING.md",
        "helix-context/docs/api/endpoints.md",
    ]
    # Only the TROUBLESHOOTING entry is delivered; under single-gold
    # (gold = ["helix-context/helix.toml"]) this miss would silently
    # underestimate retrieval quality.
    delivered = ["F:/Projects/helix-context/docs/TROUBLESHOOTING.md"]
    assert _gold_delivered(delivered, gold) is True


# ─── Single-gold backward compatibility ───────────────────────────────


def test_single_gold_legacy_hit_still_works():
    """A 1-item gold_source list must behave identically to the
    pre-multi-gold schema -- existing JSONL captures remain comparable."""
    gold = ["helix-context/helix.toml"]
    delivered = ["F:/Projects/helix-context/helix.toml"]
    assert _gold_delivered(delivered, gold) is True


def test_single_gold_legacy_miss_still_works():
    gold = ["helix-context/helix.toml"]
    delivered = ["F:/Projects/Education/CLAUDE.md"]
    assert _gold_delivered(delivered, gold) is False


# ─── Path normalization edge cases ────────────────────────────────────


def test_match_is_case_insensitive():
    """Windows mixed-case paths must match lowercase gold substrings.

    The crawler may emit ``F:/Projects/Helix-Context/...`` (mixed case
    from the on-disk dir) while the gold label is lowercase
    ``helix-context/...``."""
    gold = ["helix-context/helix.toml"]
    delivered = ["F:/Projects/Helix-Context/HELIX.TOML"]
    assert _gold_delivered(delivered, gold) is True


def test_match_normalizes_backslashes_to_forward_slashes():
    """Native Windows paths (``F:\\Projects\\...``) must match the
    forward-slash gold convention."""
    gold = ["helix-context/helix.toml"]
    delivered = ["F:\\Projects\\helix-context\\helix.toml"]
    assert _gold_delivered(delivered, gold) is True


def test_match_handles_directory_substring_gold():
    """A gold-source entry that is a directory substring (e.g.
    ``helix-context/docs``) must match any file under that directory.

    Used by the ``genome_compression_target`` needle to accept any
    ``docs/...`` file as evidence the docs root was retrieved."""
    gold = ["helix-context/docs"]
    delivered = [
        "F:/Projects/helix-context/docs/architecture/OBSERVABILITY.md"
    ]
    assert _gold_delivered(delivered, gold) is True


# ─── Schema sanity: every needle is a multi-valid-gold list ───────────


def test_matrix_needles_all_have_list_gold_source():
    """bench_claude_matrix.NEEDLES must use the list schema across the
    board -- a regression to single-string would break the ANY-match
    contract.

    Size assertion is a floor (>= 10), not exact: the N=50 expansion
    (2026-05-15 PR feat/bench-needles-50) grew the set without changing
    schema. The floor catches accidental list truncation while still
    allowing future growth.
    """
    assert len(MATRIX_NEEDLES) >= 10, (
        f"MATRIX_NEEDLES truncated to {len(MATRIX_NEEDLES)} (< 10 floor)"
    )
    for n in MATRIX_NEEDLES:
        assert isinstance(n["gold_source"], list), (
            f"{n['name']}: gold_source must be a list, got {type(n['gold_source'])}"
        )
        assert len(n["gold_source"]) >= 1, (
            f"{n['name']}: gold_source list must not be empty"
        )


def test_needle_legacy_needles_all_have_list_gold_source():
    """Same contract for the legacy bench_needle.NEEDLES list."""
    assert len(NEEDLE_NEEDLES) >= 10, (
        f"NEEDLE_NEEDLES truncated to {len(NEEDLE_NEEDLES)} (< 10 floor)"
    )
    for n in NEEDLE_NEEDLES:
        assert isinstance(n["gold_source"], list), (
            f"{n['name']}: gold_source must be a list, got {type(n['gold_source'])}"
        )
        assert len(n["gold_source"]) >= 1, (
            f"{n['name']}: gold_source list must not be empty"
        )


def test_matrix_and_needle_needles_have_matching_gold():
    """The 10 needles are duplicated across bench_claude_matrix.py and
    bench_needle.py. Their gold_source lists must stay in sync so the
    two harnesses report comparable retrieval-hit rates."""
    matrix_by_name = {n["name"]: n for n in MATRIX_NEEDLES}
    needle_by_name = {n["name"]: n for n in NEEDLE_NEEDLES}
    assert matrix_by_name.keys() == needle_by_name.keys()
    for name, matrix_n in matrix_by_name.items():
        needle_n = needle_by_name[name]
        assert matrix_n["gold_source"] == needle_n["gold_source"], (
            f"{name}: gold_source drift between bench_claude_matrix.py "
            f"and bench_needle.py:\n"
            f"  matrix : {matrix_n['gold_source']}\n"
            f"  needle : {needle_n['gold_source']}"
        )


def test_bench_answer_key_doc_never_in_gold_source():
    """``docs/benchmarks/BENCHMARKS.md`` is the bench answer-key file --
    it must NEVER be listed as a valid source. Including it would
    inflate retrieval-hit rates whenever the answer-key doc is
    retrieved, which is circular."""
    banned = "docs/benchmarks/benchmarks.md"
    for n in MATRIX_NEEDLES + NEEDLE_NEEDLES:
        for gs in n["gold_source"]:
            assert banned not in gs.lower(), (
                f"{n['name']}: gold_source contains the bench answer-key "
                f"file {gs!r}; remove it (see docs/benchmarks/MULTI_VALID_GOLD.md)"
            )
