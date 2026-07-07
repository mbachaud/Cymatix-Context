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
        "F:/Projects/OtherRepo/CLAUDE.md",
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


# ─── find_needle robustness (review 2026-07-05) ───────────────────────
#
# Pass-1 of the sike bedsweep runs find_needle with a 300s-class client
# while the answer step (Step 2, /v1/chat/completions) can legitimately
# run up to the server's upstream timeout. A Step-2 exception must NOT
# destroy the Step-1 retrieval fields — dropping the row silently
# shrinks the gold_delivered_rate denominator.


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else [{}]

    def json(self):
        return self._payload


class _FakeClient:
    """Captures /context POST bodies; raises on the answer step."""

    def __init__(self):
        self.posts = []

    def post(self, url, json=None, **kwargs):
        self.posts.append((url, json))
        if url.endswith("/context"):
            return _FakeResponse(200, [{
                "content": (
                    "<expressed_context>\n"
                    "[gene=aaaa11112222 ◆ fired=lex:1.0 100c]\n"
                    "The helix proxy listens on port 11437.\n"
                    "</expressed_context>"
                ),
                "context_health": {"status": "aligned", "ellipticity": 0.1},
                "agent": {"citations": [
                    {"gene_id": "aaaa11112222aaaa",
                     "source": "helix-context/helix.toml"},
                ]},
            }])
        raise TimeoutError("simulated answer-step ReadTimeout")


def test_find_needle_survives_answer_step_failure():
    """A Step-2 (answer accuracy) exception must not lose Step-1
    retrieval results: gold_delivered stays True, answer_correct is
    False, and no exception escapes."""
    import bench_needle

    client = _FakeClient()
    needle = {
        "name": "helix_port",
        "query": "what port does the helix proxy listen on",
        "expected": "11437",
        "accept": ["11437"],
        "gold_source": ["helix-context/helix.toml"],
    }
    r = bench_needle.find_needle(client, needle)
    assert r["gold_delivered"] is True
    assert r["found_in_context"] is True
    assert r["answer_correct"] is False


def test_find_needle_passes_ignore_delivered():
    """Bench queries must set ignore_delivered so session-delivery
    elision (default-on in production configs) can't replace gold
    bodies with stubs mid-battery (CLAUDE.md bench guidance)."""
    import bench_needle

    client = _FakeClient()
    needle = {
        "name": "helix_port",
        "query": "what port does the helix proxy listen on",
        "expected": "11437",
        "accept": ["11437"],
        "gold_source": ["helix-context/helix.toml"],
    }
    bench_needle.find_needle(client, needle)
    ctx_posts = [j for (u, j) in client.posts if u.endswith("/context")]
    assert ctx_posts, "find_needle must POST /context"
    assert ctx_posts[0].get("ignore_delivered") is True


# ─── content_has_answer: honest deliverability (2026-07-06) ───────────
#
# body_has_answer relies on citation->body pairing, which fails closed to
# empty bodies under the legibility-off probe (root-caused 2026-07-06:
# 17/23 xl "gold delivered but body missing" needles had the answer in the
# assembled content all along). content_has_answer word-boundary-matches the
# FULL content the model actually reads, so it recovers those hits.


def test_content_has_answer_recovers_answer_outside_block_bodies():
    """The undercount case: the answer is in the assembled content but NOT in
    any parsed <GENE> body -> body_has_answer=False, content_has_answer=True."""
    import bench_needle
    content = (
        "<expressed_context>\n"
        "note: the proxy listens on 11437 by default\n"
        '<GENE src="helix-context/helix.toml">unrelated config body</GENE>\n'
        "</expressed_context>"
    )
    r = bench_needle.check_gold_delivery(
        content, ["helix-context/helix.toml"], ["11437"])
    assert r["gold_delivered"] is True       # gold source delivered
    assert r["body_has_answer"] is False     # not in the gold block's body
    assert r["content_has_answer"] is True   # but in the content the model reads


def test_content_has_answer_word_boundary_guard():
    """content_has_answer must not count 11434 as an 11437 hit (the
    localhost:11434 false-positive the word-boundary rule exists to kill)."""
    import bench_needle
    content = "<expressed_context>\nupstream is http://localhost:11434\n</expressed_context>"
    r = bench_needle.check_gold_delivery(
        content, ["helix-context/helix.toml"], ["11437"])
    assert r["content_has_answer"] is False


def test_content_has_answer_true_when_body_also_has_it():
    """When the answer IS in a delivered body, both metrics agree."""
    import bench_needle
    content = (
        '<GENE src="helix-context/helix.toml">port = 11437  # proxy</GENE>'
    )
    r = bench_needle.check_gold_delivery(
        content, ["helix-context/helix.toml"], ["11437"])
    assert r["body_has_answer"] is True
    assert r["content_has_answer"] is True
