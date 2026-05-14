"""Shared citation parser for benchmarks + diagnostics.

The modern `/context` response carries structured per-document metadata
at ``response[0]["agent"]["citations"]``:

    [
      {
        "name": "Helix Genome Context",
        "content": "[gene=abc12345... ◆ fired=harmonic:2.3 1200→320c]\nspliced text...",
        "agent": {
          "citations": [
            {"gene_id": "abc12345...", "source": "path/to/file.py", "score": 1.42},
            ...
          ],
          ...
        }
      }
    ]

Historical JSONL/results files (pre-2026-05) embedded each delivered
document as ``<GENE src="path/to/file.py" ...>...</GENE>`` inline in the
``content``/``expressed_context`` string. The renderer no longer emits
that markup -- it was replaced by ``[gene=...]`` legibility headers
(see :mod:`helix_context.encoding.legibility`) -- but old JSONL captures
must remain inspectable.

This module is the single place benchmark/diagnostic code should look up
"what sources did Helix actually deliver?". It prefers the structured
``agent.citations`` payload and falls back to the legacy regex only when
no structured citations are present.

Issue: https://github.com/mbachaud/helix-context/issues/101
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Legacy assembly markup. Retained ONLY as a fallback for historical
# JSONL files captured before the legibility header migration. The live
# renderer no longer emits this -- new code must NOT regex this against
# fresh /context responses.
LEGACY_GENE_SRC_RE = re.compile(r'<GENE src="([^"]+)"[^>]*>', re.DOTALL)
LEGACY_GENE_BLOCK_RE = re.compile(
    r'<GENE src="([^"]+)"[^>]*>(.*?)</GENE>', re.DOTALL,
)


def _coerce_top_entry(payload: Any) -> dict:
    """Return the first dict-like entry from a /context response.

    `/context` returns a list (``[response]``). Some historical captures
    serialized just the inner dict. Accept either form so callers can
    pass `response.json()` directly without unwrapping.
    """
    if isinstance(payload, list):
        return payload[0] if payload and isinstance(payload[0], dict) else {}
    if isinstance(payload, dict):
        return payload
    return {}


def _extract_content(entry: dict) -> str:
    """Best-effort content string for legacy fallback parsing."""
    return (
        entry.get("content")
        or entry.get("expressed_context")
        or ""
    )


def extract_citations(payload: Any) -> list[dict]:
    """Return the structured ``agent.citations`` list, or [].

    Preferred entry point for benchmarks that want per-document metadata
    (gene_id + source + score). Returns ``[]`` for unrecognized payloads.
    """
    entry = _coerce_top_entry(payload)
    agent = entry.get("agent") or {}
    citations = agent.get("citations")
    if isinstance(citations, list):
        # Defensive: only keep dict entries
        return [c for c in citations if isinstance(c, dict)]
    return []


def extract_sources(payload: Any) -> list[str]:
    """Return delivered source identifiers from a /context response.

    Priority:
      1. ``agent.citations[].source`` -- modern, structured (since
         routes_context.py introduced the citations payload).
      2. Legacy ``<GENE src="...">`` regex against the content string
         -- only used when no structured citations exist (historical
         JSONL replays).

    Empty / blank source strings are dropped. Order is preserved.
    Duplicates are NOT deduplicated -- callers can dedupe if they need
    set semantics; preserving order + multiplicity matches what the
    previous regex-based code returned.
    """
    citations = extract_citations(payload)
    if citations:
        sources = [str(c.get("source") or "") for c in citations]
        return [s for s in sources if s]

    # Legacy fallback (historical JSONL files only).
    entry = _coerce_top_entry(payload)
    content = _extract_content(entry)
    if not content:
        return []
    return LEGACY_GENE_SRC_RE.findall(content)


def extract_gene_ids(payload: Any) -> list[str]:
    """Return delivered gene_ids from the structured citations payload.

    Legacy ``<GENE src=...>`` markup did not include gene_ids, so the
    fallback path returns ``[]``.
    """
    citations = extract_citations(payload)
    return [str(c.get("gene_id") or "") for c in citations if c.get("gene_id")]


def parse_legacy_gene_blocks(content: str) -> list[tuple[str, str]]:
    """Return (src, body) tuples from legacy ``<GENE src=...>`` markup.

    Used by bench_needle.py's body-substring checks against historical
    JSONL replays. For modern responses, the per-document body is not
    inline in the content blob -- callers that need body text should
    re-fetch via /context/expand or read the source file directly.
    """
    if not content:
        return []
    return LEGACY_GENE_BLOCK_RE.findall(content)


# Modern legibility header (see helix_context/encoding/legibility.py):
#   [gene=abc12345... ◆ fired=harmonic:2.3,lex_anchor:1.1 1200→320c]
# The trailing chars in the gene_id may include any non-bracket chars; the
# id field stops at the first space (legibility writes ``[gene={short} {symbol}``).
LEGIBILITY_HEADER_RE = re.compile(r"\[gene=([^\s\]]+)[^\]]*\]", re.DOTALL)


def extract_block_bodies(payload: Any) -> list[tuple[str, str, str]]:
    """Return (source, gene_id, body) tuples for every delivered document.

    For modern responses, the content blob is the assembled splice
    output -- one legibility header line followed by spliced text, blocks
    separated by ``\\n---\\n``. We split on ``---``, strip the header
    from each block, and pair each body with the matching
    ``agent.citations`` entry (by order, which is the contract: the
    renderer writes citations in delivery order).

    For legacy responses, falls back to ``<GENE src=...>...</GENE>``
    block extraction with empty gene_ids.

    Used by bench_needle.py to perform word-boundary matches against
    each delivered document body (the "what the consumer actually sees"
    metric).
    """
    citations = extract_citations(payload)
    entry = _coerce_top_entry(payload)
    content = _extract_content(entry)

    if citations:
        # Split content on the per-block separator. Some prefix material
        # (e.g. an outer ``<expressed_context>`` wrapper) may precede the
        # first block; ignore blocks whose body doesn't start with a
        # legibility header, since they cannot be confidently paired.
        raw_blocks = (content or "").split("\n---\n")
        paired: list[tuple[str, str, str]] = []
        body_iter = iter(raw_blocks)
        for cit_obj in citations:
            source = str(cit_obj.get("source") or "")
            gene_id = str(cit_obj.get("gene_id") or "")
            body = ""
            for raw in body_iter:
                stripped = raw.lstrip()
                if LEGIBILITY_HEADER_RE.match(stripped):
                    # Remove the header line from the body.
                    body = LEGIBILITY_HEADER_RE.sub("", stripped, count=1).lstrip("\n")
                    break
            paired.append((source, gene_id, body))
        return paired

    # Legacy fallback: gene_id was never in the markup.
    return [(src, "", body) for src, body in parse_legacy_gene_blocks(content)]


def normalize_sources(sources: Iterable[str]) -> list[str]:
    """Forward-slash normalize + lowercase for path comparison.

    Convenience wrapper for callers that compare delivered paths
    against gold-source substrings.
    """
    return [(s or "").replace("\\", "/").lower() for s in sources]
