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
#   [gene=abc12345abc1 ◆ fired=harmonic:2.3,lex_anchor:1.1 1200→320c]
# The captured id is ``gene_id[:12]`` — a plain prefix of the full
# citation gene_id (id_width=12 in both format_gene_header and the
# session-delivery elision stub). The id field stops at the first space.
LEGIBILITY_HEADER_RE = re.compile(r"\[gene=([^\s\]]+)[^\]]*\]", re.DOTALL)

# Gene ids are sha256 hexdigest[:16] (knowledge_store.py), so a real
# header short-id is 8-16 lowercase hex chars. Doc prose quoting the
# header shape (e.g. "[gene=... ◆ fired=...]" examples in api docs) must
# not be misparsed as a header — a bogus short-id would strip the
# block's first line and poison the id join (review 2026-07-05).
_HEX_SHORT_ID_RE = re.compile(r"[0-9a-f]{8,16}", re.IGNORECASE)

# Session-delivery elision stubs are all-header lines:
#   [gene=abc12345abc1 ↻ delivered 3 queries ago / 45s — see earlier response]
# Their text must never be used as a body: bare integers in the stub
# word-boundary-match numeric accepts ("delivered 16 queries ago" vs
# accept "16"), silently inflating body_has_answer (review 2026-07-05).
_ELISION_STUB_MARKER = "see earlier response"

# Wrapper emitted by context_manager around the assembled blocks. The
# live /context ``content`` is ALWAYS the wrapped string — splitting on
# the block separator leaves the open tag glued to the FIRST block, so
# it must be stripped before header matching (2026-07-04: unstripped, it
# made the first block unpairable and shifted every citation→body pair
# by one, flat-lining body_has_answer across all sike_bedsweep runs).
_EXPRESSED_OPEN = "<expressed_context>"
_EXPRESSED_CLOSE = "</expressed_context>"


def _strip_expressed_wrapper(content: str) -> str:
    text = (content or "").strip()
    if text.startswith(_EXPRESSED_OPEN):
        text = text[len(_EXPRESSED_OPEN):]
    if text.endswith(_EXPRESSED_CLOSE):
        text = text[: -len(_EXPRESSED_CLOSE)]
    return text.strip("\n")


def extract_block_bodies(payload: Any) -> list[tuple[str, str, str]]:
    """Return (source, gene_id, body) tuples for every delivered document.

    For modern responses, the content blob is the assembled splice
    output wrapped in ``<expressed_context>`` tags -- one legibility
    header line followed by spliced text, blocks separated by
    ``\\n---\\n``. Pairing strategy, in order of trust:

    1. Id join: a block's ``[gene={id[:12]} ...]`` header (8-16 hex
       chars) is a prefix of exactly one citation's full gene_id.
       Survives citations dropped by the server's row-lookup miss
       (routes_context ``continue``s on a missing row, so citations can
       be FEWER than blocks).
    2. Positional, count-guarded: headerless blocks (e.g. a bench
       config with ``legibility_enabled = false``) pair by index ONLY
       when their count equals the count of citations left unmatched by
       the id join. On any mismatch (dropped citation, a Markdown
       horizontal rule ``\\n---\\n`` inside a body inflating the block
       count) pairing is ambiguous and the affected bodies are returned
       as ``""`` — fail closed rather than silently mispair.

    Elision stubs (session working-set) pair by id like any block but
    always yield ``body=""``: their text ("delivered 16 queries ago")
    would otherwise word-boundary-match numeric accept strings.

    For legacy responses, falls back to ``<GENE src=...>...</GENE>``
    block extraction with empty gene_ids.

    Used by bench_needle.py to perform word-boundary matches against
    each delivered document body (the "what the consumer actually sees"
    metric).
    """
    citations = extract_citations(payload)
    entry = _coerce_top_entry(payload)
    content = _extract_content(entry)

    if not citations:
        # Legacy fallback: gene_id was never in the markup.
        return [(src, "", body) for src, body in parse_legacy_gene_blocks(content)]

    inner = _strip_expressed_wrapper(content)
    raw_blocks = inner.split("\n---\n") if inner else []

    # Parse blocks into (header_short_id | None, body). A header is only
    # trusted when its captured id is hex-shaped; otherwise the block is
    # treated as headerless with its FULL text (a doc example like
    # "[gene=... ◆ ...]" quoted at line start must not be stripped).
    parsed: list[tuple[str | None, str]] = []
    for raw in raw_blocks:
        stripped = raw.strip()
        m = LEGIBILITY_HEADER_RE.match(stripped)
        if m and _HEX_SHORT_ID_RE.fullmatch(m.group(1)):
            if _ELISION_STUB_MARKER in m.group(0) or "↻" in m.group(0):
                # Elision stub: pairs by id, but never contributes body
                # text (numeric accept collision — see marker comment).
                parsed.append((m.group(1), ""))
            else:
                body = LEGIBILITY_HEADER_RE.sub("", stripped, count=1).lstrip("\n")
                parsed.append((m.group(1), body))
        else:
            parsed.append((None, stripped))

    # Pass 1 — id join: header short-id is a prefix of the citation's
    # full gene_id. Each block is claimed at most once.
    assigned: dict[int, int] = {}  # citation index -> block index
    unclaimed = list(range(len(parsed)))
    for ci, cit_obj in enumerate(citations):
        gid = str(cit_obj.get("gene_id") or "")
        if not gid:
            continue
        for bi in unclaimed:
            short = parsed[bi][0]
            if short and gid.startswith(short):
                assigned[ci] = bi
                unclaimed.remove(bi)
                break

    # Pass 2 — positional fallback, count-guarded. Positional pairing
    # assumes blocks:citations are 1:1 in delivery order; that breaks
    # when the server drops a citation (row-lookup miss) or a headerless
    # body contains "\n---\n" (Markdown hr) and inflates the block
    # count. Only pair when the counts line up exactly; otherwise leave
    # bodies empty — an empty body is a visible measurement gap, a
    # mispaired body is a silent lie. Never hand out an unmatched
    # HEADERED block positionally: it belongs to a different document.
    unmatched = [ci for ci in range(len(citations)) if ci not in assigned]
    if assigned:
        candidates = [bi for bi in unclaimed if parsed[bi][0] is None]
    else:
        candidates = unclaimed
    fallback_blocks = candidates if len(candidates) == len(unmatched) else []
    fallback_iter = iter(fallback_blocks)

    paired: list[tuple[str, str, str]] = []
    for ci, cit_obj in enumerate(citations):
        source = str(cit_obj.get("source") or "")
        gene_id = str(cit_obj.get("gene_id") or "")
        if ci in assigned:
            body = parsed[assigned[ci]][1]
        else:
            bi = next(fallback_iter, None)
            body = parsed[bi][1] if bi is not None else ""
        paired.append((source, gene_id, body))
    return paired


def normalize_sources(sources: Iterable[str]) -> list[str]:
    """Forward-slash normalize + lowercase for path comparison.

    Convenience wrapper for callers that compare delivered paths
    against gold-source substrings.
    """
    return [(s or "").replace("\\", "/").lower() for s in sources]
