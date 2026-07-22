"""Regression tests for the shared benchmark citation parser.

Closes https://github.com/mbachaud/helix-context/issues/101 -- the bench
parsers were still regexing for legacy ``<GENE src="...">`` markup that
the live renderer no longer emits, causing retrieval hit-rates to look
artificially low. The helper now prefers ``agent.citations[].source``
and falls back to the legacy regex only for historical JSONL inputs.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make benchmarks/ importable without packaging it (matches the
# convention used by test_bench_harvest.py).
BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

import _citations as cit  # noqa: E402


# ─── Fixtures: minimal but realistic /context response shapes ──────────


def _modern_response() -> list[dict]:
    """Approximation of the live /context response shape.

    Source: cymatix_context/server/routes_context.py builds ``response``
    as a dict and returns ``[response]`` at line 533. The ``agent.citations``
    payload is populated from genes table rows (see lines 317-359).
    """
    return [{
        "name": "Helix Genome Context",
        "description": "2 genes expressed, 4.2x compression",
        "content": (
            "[gene=abc12345... ◆ fired=harmonic:2.3,lex_anchor:1.1 1200→320c]\n"
            "spliced text from document 1\n"
            "---\n"
            "[gene=def67890... ◇ fired=sema_boost:1.8 180c]\n"
            "spliced text from document 2\n"
        ),
        "context_health": {"status": "aligned", "ellipticity": 0.32},
        "agent": {
            "recommendation": "trust",
            "hint": "Context is well-grounded. Use directly.",
            "citations": [
                {
                    "gene_id": "abc12345abc12345",
                    "source": "helix-context/helix.toml",
                    "score": 1.42,
                },
                {
                    "gene_id": "def67890def67890",
                    "source": "helix-context/README.md",
                    "score": 0.93,
                },
            ],
        },
    }]


def _legacy_response() -> list[dict]:
    """Pre-migration capture: legibility headers + agent.citations absent.

    This is what historical JSONL files look like -- only the inline
    ``<GENE src=...>...</GENE>`` markup survives in the content blob.
    """
    return [{
        "name": "Helix Genome Context",
        "content": (
            "<expressed_context>\n"
            '<GENE src="helix-context/helix.toml" facts="port=11437">\n'
            "The helix proxy listens on port 11437.\n"
            "</GENE>\n"
            '<GENE src="helix-context/README.md" facts="pipeline=6">\n'
            "Six-step expression pipeline.\n"
            "</GENE>\n"
            "</expressed_context>\n"
        ),
        "context_health": {"status": "aligned"},
    }]


# ─── Modern shape: structured citations win ────────────────────────────


def test_modern_response_returns_citation_sources():
    sources = cit.extract_sources(_modern_response())
    assert sources == [
        "helix-context/helix.toml",
        "helix-context/README.md",
    ]


def test_modern_response_returns_gene_ids():
    ids = cit.extract_gene_ids(_modern_response())
    assert ids == ["abc12345abc12345", "def67890def67890"]


def test_modern_response_returns_full_citation_dicts():
    citations = cit.extract_citations(_modern_response())
    assert len(citations) == 2
    assert citations[0]["source"] == "helix-context/helix.toml"
    assert citations[0]["score"] == 1.42


def test_modern_response_unwrapped_dict_also_works():
    """Some callers may pass response[0] directly instead of the wrapper list."""
    entry = _modern_response()[0]
    assert cit.extract_sources(entry) == [
        "helix-context/helix.toml",
        "helix-context/README.md",
    ]


# ─── Legacy fallback: regex on content string ──────────────────────────


def test_legacy_response_falls_back_to_regex():
    """No agent.citations → use <GENE src="..."> markup from content."""
    sources = cit.extract_sources(_legacy_response())
    assert sources == [
        "helix-context/helix.toml",
        "helix-context/README.md",
    ]


def test_legacy_response_no_gene_ids_returned():
    """Legacy markup doesn't carry gene_ids; helper must return empty."""
    ids = cit.extract_gene_ids(_legacy_response())
    assert ids == []


def test_legacy_block_parser_returns_src_and_body():
    """parse_legacy_gene_blocks is used by needle body-substring checks."""
    content = _legacy_response()[0]["content"]
    blocks = cit.parse_legacy_gene_blocks(content)
    assert len(blocks) == 2
    assert blocks[0][0] == "helix-context/helix.toml"
    assert "port 11437" in blocks[0][1]


# ─── Edge cases: empty / malformed inputs must not crash ───────────────


def test_empty_response_returns_empty_list():
    assert cit.extract_sources([]) == []
    assert cit.extract_sources({}) == []
    assert cit.extract_sources(None) == []


def test_empty_citations_array_returns_empty_list():
    payload = [{"content": "", "agent": {"citations": []}}]
    assert cit.extract_sources(payload) == []


def test_citations_with_empty_source_strings_filtered():
    payload = [{
        "agent": {
            "citations": [
                {"gene_id": "a", "source": ""},
                {"gene_id": "b", "source": "real/path.py"},
                {"gene_id": "c"},  # missing source key
            ]
        }
    }]
    assert cit.extract_sources(payload) == ["real/path.py"]


def test_no_content_and_no_citations_returns_empty():
    """A degenerate response with neither structured citations nor any
    content string must not raise."""
    payload = [{"name": "Helix Genome Context"}]
    assert cit.extract_sources(payload) == []
    assert cit.extract_citations(payload) == []
    assert cit.extract_gene_ids(payload) == []


def test_unrecognized_payload_type_returns_empty():
    assert cit.extract_sources("not a dict or list") == []
    assert cit.extract_sources(42) == []


def test_normalize_sources_lowercases_and_forward_slashes():
    raw = ["Helix-Context\\helix.toml", "EDUCATION/CLAUDE.md", ""]
    assert cit.normalize_sources(raw) == [
        "helix-context/helix.toml",
        "education/claude.md",
        "",
    ]


# ─── Cross-check: agent.citations always wins over inline legacy markup ──


def test_modern_response_with_legacy_markup_still_in_content():
    """A response that has BOTH structured citations AND (somehow)
    leftover legacy markup in content must prefer the structured path."""
    payload = [{
        "content": '<GENE src="WRONG/legacy.py">stale</GENE>',
        "agent": {
            "citations": [
                {"gene_id": "x", "source": "CORRECT/modern.py"},
            ],
        },
    }]
    assert cit.extract_sources(payload) == ["CORRECT/modern.py"]


# ─── Block-body extraction: pair citations with spliced text ───────────


def test_modern_block_bodies_paired_with_citations():
    """Modern responses: split content on ``---``, strip legibility
    headers, pair each body with its citation by order."""
    blocks = cit.extract_block_bodies(_modern_response())
    assert len(blocks) == 2
    assert blocks[0][0] == "helix-context/helix.toml"        # source
    assert blocks[0][1] == "abc12345abc12345"                 # gene_id
    assert "spliced text from document 1" in blocks[0][2]    # body
    assert blocks[1][0] == "helix-context/README.md"
    assert "spliced text from document 2" in blocks[1][2]


def test_legacy_block_bodies_fallback():
    """Legacy responses: bodies come from <GENE>...</GENE>; gene_id empty."""
    blocks = cit.extract_block_bodies(_legacy_response())
    assert len(blocks) == 2
    assert blocks[0][0] == "helix-context/helix.toml"
    assert blocks[0][1] == ""  # legacy markup has no gene_id
    assert "port 11437" in blocks[0][2]


def test_empty_block_bodies_no_crash():
    assert cit.extract_block_bodies([]) == []
    assert cit.extract_block_bodies({}) == []


# ─── Realistic live shapes: <expressed_context> wrapper (2026-07-04) ────
#
# The live renderer ships ``window.expressed_context`` == the WRAPPED
# string (context_manager.py builds ``<expressed_context>\n...\n
# </expressed_context>``). The fixtures above omit the wrapper, which is
# why the old tests stayed green while every real /context response
# failed body pairing: the wrapper glues to the first block, the header
# regex (anchored via .match) misses it, the first body is dropped, and
# every citation→body pairing shifts by one. Observed as
# body_has_answer_rate ∈ {0.0, 0.02} across ALL sike_bedsweep runs.


def _wrapped_response(with_headers: bool = True) -> list[dict]:
    """The live /context response shape: wrapper + optional headers.

    ``with_headers=False`` mirrors bench configs that set
    ``legibility_enabled = false`` (e.g. docs/benchmarks/
    helix_probe_lexical.toml): parts are bare spliced text.
    """
    if with_headers:
        content = (
            "<expressed_context>\n"
            "[gene=aaaa11112222 ◆ fired=harmonic:2.3,lex_anchor:1.1 1200→320c]\n"
            "The helix proxy listens on port 11437.\n"
            "---\n"
            "[gene=bbbb33334444 ◇ fired=sema_boost:1.8 180c]\n"
            "Six-step expression pipeline.\n"
            "</expressed_context>"
        )
    else:
        content = (
            "<expressed_context>\n"
            "The helix proxy listens on port 11437.\n"
            "---\n"
            "Six-step expression pipeline.\n"
            "</expressed_context>"
        )
    return [{
        "name": "Helix Genome Context",
        "content": content,
        "agent": {
            "citations": [
                {"gene_id": "aaaa11112222aaaa", "source": "helix-context/helix.toml"},
                {"gene_id": "bbbb33334444bbbb", "source": "helix-context/README.md"},
            ],
        },
    }]


def test_wrapped_content_first_block_body_not_lost():
    """Regression: the <expressed_context> wrapper must not swallow the
    first block. Before the fix, blocks[0] body was '' and pairing
    shifted by one."""
    blocks = cit.extract_block_bodies(_wrapped_response())
    assert len(blocks) == 2
    assert blocks[0][0] == "helix-context/helix.toml"
    assert "port 11437" in blocks[0][2]
    assert blocks[1][0] == "helix-context/README.md"
    assert "Six-step" in blocks[1][2]


def test_wrapped_content_close_tag_not_in_last_body():
    blocks = cit.extract_block_bodies(_wrapped_response())
    for _, _, body in blocks:
        assert "</expressed_context>" not in body


def test_headerless_content_pairs_positionally():
    """legibility_enabled=false configs emit no [gene=...] headers.
    Bodies must still pair positionally (citations are written in
    delivery order, 1:1 with content blocks) instead of all-empty."""
    blocks = cit.extract_block_bodies(_wrapped_response(with_headers=False))
    assert len(blocks) == 2
    assert "port 11437" in blocks[0][2]
    assert "Six-step" in blocks[1][2]


def test_dropped_citation_does_not_shift_pairing():
    """routes_context skips a citation when its row lookup fails
    (``continue`` at the row_map miss), so citations can be FEWER than
    content blocks. Header short-ids (gene_id[:12]) are prefixes of the
    full citation gene_id — pairing must join on that, not position."""
    payload = [{
        "content": (
            "<expressed_context>\n"
            "[gene=aaaa11112222 ◆ fired=lex:1.0 100c]\n"
            "body of gene A\n"
            "---\n"
            "[gene=bbbb33334444 ◇ fired=lex:0.9 90c]\n"
            "body of gene B (citation row was dropped)\n"
            "---\n"
            "[gene=cccc55556666 ⬦ fired=lex:0.8 80c]\n"
            "body of gene C\n"
            "</expressed_context>"
        ),
        "agent": {
            "citations": [
                {"gene_id": "aaaa11112222aaaa", "source": "a.py"},
                # gene bbbb... citation dropped by the server
                {"gene_id": "cccc55556666cccc", "source": "c.py"},
            ],
        },
    }]
    blocks = cit.extract_block_bodies(payload)
    assert len(blocks) == 2
    assert blocks[0][0] == "a.py"
    assert "body of gene A" in blocks[0][2]
    assert blocks[1][0] == "c.py"
    assert "body of gene C" in blocks[1][2]


def test_elision_stub_body_is_empty():
    """Session-delivery elision stubs share the [gene={id[:12]} ...]
    shape and must pair with their citation — but their body must be ''
    so accept-matching can never fire on stub text. (Review 2026-07-05:
    'delivered 16 queries ago' word-boundary matches numeric accepts
    like '16'; '1' matches the age string '1.2h'.)"""
    payload = [{
        "content": (
            "<expressed_context>\n"
            "[gene=aaaa11112222 ↻ delivered 3 queries ago / 45s — see earlier response]\n"
            "---\n"
            "[gene=bbbb33334444 ◆ fired=lex:1.2 200→90c]\n"
            "fresh body text\n"
            "</expressed_context>"
        ),
        "agent": {
            "citations": [
                {"gene_id": "aaaa11112222aaaa", "source": "elided.py"},
                {"gene_id": "bbbb33334444bbbb", "source": "fresh.py"},
            ],
        },
    }]
    blocks = cit.extract_block_bodies(payload)
    assert blocks[0][0] == "elided.py"
    assert blocks[0][2] == ""
    assert blocks[1][0] == "fresh.py"
    assert "fresh body text" in blocks[1][2]


def test_elision_stub_numeric_accept_does_not_inflate_metrics():
    """The real collision: helix_subpackages_count accepts '16', and a
    stub reading 'delivered 16 queries ago' must NOT count as
    body_has_answer/gold_has_answer."""
    import bench_needle

    payload = [{
        "content": (
            "<expressed_context>\n"
            "[gene=aaaa11112222 ↻ delivered 16 queries ago / 1.2h — see earlier response]\n"
            "</expressed_context>"
        ),
        "agent": {
            "citations": [
                {"gene_id": "aaaa11112222aaaa", "source": "helix-context/CLAUDE.md"},
            ],
        },
    }]
    gold = bench_needle.check_gold_delivery(
        "", ["helix-context/CLAUDE.md"], ["16", "1"], response=payload,
    )
    assert gold["gold_delivered"] is True   # citation still counts
    assert gold["gold_has_answer"] is False
    assert gold["body_has_answer"] is False


def test_headerless_count_mismatch_fails_closed():
    """Headerless (legibility off) pairing is positional and only safe
    when blocks and citations are 1:1. A dropped citation (server
    row-lookup miss) must yield empty bodies, not shifted ones."""
    payload = [{
        "content": (
            "<expressed_context>\n"
            "body of gene X\n"
            "---\n"
            "body of gene Y\n"
            "</expressed_context>"
        ),
        "agent": {
            # X's citation was dropped by the server; naive positional
            # pairing would hand Y gene X's body.
            "citations": [{"gene_id": "yyyy77778888yyyy", "source": "y.py"}],
        },
    }]
    blocks = cit.extract_block_bodies(payload)
    assert len(blocks) == 1
    assert blocks[0][0] == "y.py"
    assert blocks[0][2] == ""  # fail closed, never X's body


def test_headerless_markdown_hr_inflation_fails_closed():
    """A headerless body containing a Markdown horizontal rule splits
    into extra blocks (2,318/46,777 xl genes contain '\\n---\\n').
    Count mismatch must fail closed rather than shift pairings."""
    payload = [{
        "content": (
            "<expressed_context>\n"
            "intro text\n"
            "---\n"
            "conclusion of doc one\n"
            "---\n"
            "body of doc two\n"
            "</expressed_context>"
        ),
        "agent": {
            "citations": [
                {"gene_id": "aaaa11112222aaaa", "source": "one.md"},
                {"gene_id": "bbbb33334444bbbb", "source": "two.md"},
            ],
        },
    }]
    blocks = cit.extract_block_bodies(payload)
    assert len(blocks) == 2
    # 3 raw blocks vs 2 citations: pairing is ambiguous — bodies empty.
    assert blocks[0][2] == ""
    assert blocks[1][2] == ""


def test_mixed_mode_headerless_fallback_requires_count_match():
    """When SOME blocks id-matched (e.g. an elision stub) the remaining
    headerless blocks are handed out positionally ONLY if their count
    equals the unmatched-citation count."""
    base_citations = [
        {"gene_id": "aaaa11112222aaaa", "source": "stub.py"},
        {"gene_id": "bbbb33334444bbbb", "source": "b.py"},
        {"gene_id": "cccc55556666cccc", "source": "c.py"},
    ]
    content_3blocks = (
        "<expressed_context>\n"
        "[gene=aaaa11112222 ↻ delivered 2 queries ago / 30s — see earlier response]\n"
        "---\n"
        "headerless body B\n"
        "---\n"
        "headerless body C\n"
        "</expressed_context>"
    )
    # Counts align (2 unmatched citations, 2 headerless blocks) → pair.
    ok = cit.extract_block_bodies([{
        "content": content_3blocks,
        "agent": {"citations": base_citations},
    }])
    assert ok[1][2] == "headerless body B"
    assert ok[2][2] == "headerless body C"

    # Drop citation B: 1 unmatched citation vs 2 headerless blocks →
    # ambiguous → fail closed.
    dropped = cit.extract_block_bodies([{
        "content": content_3blocks,
        "agent": {"citations": [base_citations[0], base_citations[2]]},
    }])
    assert dropped[0][0] == "stub.py"
    assert dropped[1][0] == "c.py"
    assert dropped[1][2] == ""


def test_non_hex_bracket_prefix_treated_as_headerless():
    """A block that merely STARTS with bracketed text that is not a
    hex gene-id header (e.g. a quoted doc example like '[gene=WHAT ...]')
    must be treated as headerless, not stripped/misparsed."""
    payload = [{
        "content": (
            "<expressed_context>\n"
            "[gene=EXAMPLE-NOT-HEX ◆ fired=doc:1.0 10c] is the header shape\n"
            "and this line documents it.\n"
            "</expressed_context>"
        ),
        "agent": {
            "citations": [
                {"gene_id": "aaaa11112222aaaa", "source": "docs/api.md"},
            ],
        },
    }]
    blocks = cit.extract_block_bodies(payload)
    assert len(blocks) == 1
    # 1 headerless block, 1 citation → positional pairing keeps the
    # FULL text including the bracketed example.
    assert "[gene=EXAMPLE-NOT-HEX" in blocks[0][2]
    assert "documents it" in blocks[0][2]


def test_check_gold_delivery_body_has_answer_on_live_shape():
    """End-to-end regression for the dead body_has_answer metric: a
    realistic wrapped+headered response whose gold block contains the
    accept token must score body_has_answer=True."""
    import bench_needle

    gold = bench_needle.check_gold_delivery(
        "", ["helix-context/helix.toml"], ["11437"],
        response=_wrapped_response(),
    )
    assert gold["gold_delivered"] is True
    assert gold["gold_has_answer"] is True
    assert gold["body_has_answer"] is True
