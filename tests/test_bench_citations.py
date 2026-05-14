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

    Source: helix_context/server/routes_context.py builds ``response``
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
