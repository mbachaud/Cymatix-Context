"""Query-aware source windowing.

When a source file is too large to deliver whole, a head slice is often
the wrong slice. This module selects a bounded window that best matches
the expanded query terms while preserving a little surrounding context.
"""

from __future__ import annotations

import math
import re

from ..accel import expand_query_terms, extract_query_signals


def _query_terms(query: str) -> list[str]:
    domains, entities = extract_query_signals(query)
    return expand_query_terms(list(domains) + list(entities))


def _term_weight(term: str) -> float:
    # Longer, more specific anchors matter more than generic words.
    return 1.0 + min(len(term), 16) / 8.0


def best_relevance_window(
    text: str,
    query: str,
    *,
    max_chars: int = 5000,
    overlap: int = 1000,
) -> str:
    """Return the highest-scoring bounded window for ``query``.

    Scores overlapping windows by expanded query-term presence. If no
    query term occurs, falls back to a head slice so behavior remains
    predictable.
    """
    if not text or max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text

    terms = _query_terms(query)
    if not terms:
        return text[:max_chars]

    lowered = text.lower()
    step = max(1, max_chars - max(0, min(overlap, max_chars - 1)))
    best_start = 0
    best_score = -1.0

    for start in range(0, len(text), step):
        end = min(len(text), start + max_chars)
        window = lowered[start:end]
        score = 0.0
        hits = 0
        for term in terms:
            # Treat underscores and hyphens as meaningful for compound
            # needles, but still allow plain substring matches in code.
            count = window.count(term.lower())
            if count:
                hits += 1
                score += _term_weight(term) * (1.0 + math.log1p(count))
        if hits > 1:
            score *= 1.0 + min(hits, 8) / 10.0
        if score > best_score:
            best_score = score
            best_start = start
        if end >= len(text):
            break

    if best_score <= 0:
        return text[:max_chars]

    # Snap slightly backward to a line boundary when possible; this helps
    # code/spec slices start at a readable section rather than mid-token.
    start = best_start
    lookback = text[max(0, start - 300):start]
    line_break = lookback.rfind("\n")
    if line_break >= 0:
        start = max(0, start - len(lookback) + line_break + 1)
    return text[start:start + max_chars]


def annotate_window(source_id: str, window: str, original_len: int) -> str:
    """Format a source window with truncation metadata for diagnostics."""
    if original_len <= len(window):
        return f"\n\n--- SOURCE {source_id} ---\n{window}"
    return (
        f"\n\n--- SOURCE {source_id} "
        f"(query-selected {len(window)}/{original_len} chars) ---\n{window}"
    )
