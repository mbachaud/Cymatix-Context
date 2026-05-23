"""Uniform-path per-gene char-budget allocator (non-foveated splice loop).

build_context's Step-4 splice loop historically compressed every candidate gene
to a flat ``target = 1000`` chars (context_manager.py), ignoring the ~28KB of
``expression_tokens`` headroom and the gene count. On a whole-document corpus
this slices off the tail of each gene -- where specific facts live -- even when
a single retrieved gene would fit the prompt many times over.

This module computes per-gene ``target_chars`` for that loop. ``compress_text``
returns content verbatim when ``len(content) <= target_chars``, so a target at
least as large as a gene's length delivers that gene whole.

Modes
-----
"fixed"
    Every gene gets ``fixed_target``. Byte-identical to the legacy behavior.
"dynamic"
    Floor-then-greedy. Every gene is first floored at ``min(floor_chars,
    length)`` so no gene ever regresses below the legacy behavior or targets
    more than it has. The remaining budget is then handed to genes in rank
    order (input order == rank), raising each up to ``min(length,
    ceiling_chars)`` until the budget is exhausted. The ceiling stops a single
    retrieved "whale" from eating the entire prompt.

The allocator is intentionally pure (no config/torch imports) so it unit-tests
fast and in isolation; the caller passes primitives drawn from BudgetConfig.
"""
from __future__ import annotations

from typing import List, Optional, Sequence


def compute_uniform_targets(
    content_lengths: Sequence[int],
    *,
    mode: str = "fixed",
    fixed_target: int = 1000,
    total_budget_chars: int = 28000,
    ceiling_chars: int = 12000,
    floor_chars: int = 1000,
    relevance_scores: Optional[Sequence[float]] = None,
) -> List[int]:
    """Return a per-gene ``target_chars`` list, parallel to ``content_lengths``.

    Parameters
    ----------
    content_lengths:
        Character length of each candidate gene's content, in rank order
        (index 0 == top-ranked).
    mode:
        ``"fixed"`` (legacy flat target) or ``"dynamic"`` (floor-then-greedy).
        Any unrecognized value is treated as ``"fixed"`` for safety.
    fixed_target:
        Per-gene target in fixed mode (and the conceptual floor in dynamic mode
        via ``floor_chars``).
    total_budget_chars:
        Total char budget to distribute in dynamic mode (e.g.
        ``expression_tokens * 4 - overhead_reserve``).
    ceiling_chars:
        Maximum target any single gene may receive in dynamic mode.
    floor_chars:
        Minimum target each gene receives in dynamic mode (capped at its own
        length). Guarantees no gene is starved below the legacy behavior.
    relevance_scores:
        Optional parallel-to-``content_lengths`` per-gene relevance signal
        (e.g. a BM25-style query/content lex score, or a tier contribution
        like fts5). When provided, the dynamic-mode surplus is distributed
        in DESCENDING-relevance order instead of input/rank order -- the
        H10p content-aware allocator. Ties preserve input order (Python's
        ``sorted`` is stable). ``None`` (default) reproduces the legacy
        rank-order behavior byte-for-byte. Ignored in fixed mode. Must have
        the same length as ``content_lengths`` when provided in dynamic
        mode (``ValueError`` otherwise) -- a length mismatch is a caller
        bug, not a fallback condition.
    """
    n = len(content_lengths)
    if n == 0:
        return []
    if mode != "dynamic":
        return [fixed_target] * n

    if relevance_scores is not None and len(relevance_scores) != n:
        raise ValueError(
            f"relevance_scores length {len(relevance_scores)} "
            f"!= content_lengths length {n}"
        )

    lengths = [max(0, int(L)) for L in content_lengths]
    ceiling = max(0, int(ceiling_chars))
    floor = max(0, int(floor_chars))

    # Floor pass: never below the legacy target, never above the gene's length.
    targets = [min(floor, L) for L in lengths]
    remaining = max(0, int(total_budget_chars) - sum(targets))

    # Greedy surplus pass. Default order is input (rank) order. With
    # relevance_scores, descending relevance -- stable so equal-relevance
    # ties fall back to input order.
    if relevance_scores is None:
        order = range(n)
    else:
        scores = [float(s) for s in relevance_scores]
        order = sorted(range(n), key=lambda i: -scores[i])

    for i in order:
        if remaining <= 0:
            break
        L = lengths[i]
        headroom = min(L, ceiling) - targets[i]
        if headroom <= 0:
            continue
        grant = min(headroom, remaining)
        targets[i] += grant
        remaining -= grant

    return targets
