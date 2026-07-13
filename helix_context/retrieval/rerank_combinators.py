"""Post-fusion rerank combinators (Issue #255, PR-2, 2026-07-10).

The RRF fusion core (``retrieval/fusion.py``) combines the recall tiers
*correctly* — it maps each tier to ranks first, then adds rank-reciprocals,
manufacturing a shared unit before using ``+``. The four **rerank classes**
(authority / sema_boost / party_attr / access_rate) are a different story:
they are categorical facts sized on the legacy additive scale (O(0.5-2.0))
and, under RRF, get added raw onto a fused score of O(0.05-0.5). One
authority hit is ~40x a top-rank tier contribution, so the bonus *becomes*
the ranking (DEFECT-1, ``docs/research/2026-07-08-scoring-invariance-audit.md``
§3). The design record for the fix is
``docs/research/2026-07-09-scoring-combinator-exploration.md``.

This module is the finite realization of "nest, don't add": it factors the
final fused+rerank combination out of ``KnowledgeStore.query_docs`` into a
**pure function** with no store / SQL / telemetry coupling, so each combinator
can be golden-tested in isolation and A/B'd on the beds. Four operators:

``additive``
    ``final = fused + rerank``. Byte-identical to the shipped inline block —
    the default, so this knob ships inert.

``fused_tier``
    Each rerank class is fed through the same rank machinery as every recall
    tier: rank its positive-boost members by ``rank_by_score`` (the Fuser's
    ``(-score, gene_id)`` tie-break) and add ``tier_weight / (k + rank)``.
    No exchange rate to hand-pick — rank absorbs the scale. A class's maximum
    contribution is exactly ``tier_weight / (k + 1)`` (the rank-1 member).

``eps_band``
    Order by fused score; when the leaders fall within a *relative* band δ of
    each other (a fused near-tie), let rerank break the tie inside the band —
    never across a clear fused win. δ is a ratio (scale-free), not an additive
    constant. Final scores stay pure fused; only the emitted order changes.

``off``
    Pure fused ranking. The floor arm — rerank ignored entirely.

Contract: the caller (``knowledge_store.query_docs``) restricts ``fused``,
``rerank`` and ``rerank_by_class`` to the eligible id set BEFORE calling, so
this module never re-derives eligibility.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .fusion import rank_by_score

__all__ = ["combine_rerank", "resolve_class_combinator", "VALID_COMBINATORS"]

# The only valid operators. ``KnowledgeStore.__init__`` validates against this
# same set so a typo in helix.toml fails fast at construction.
VALID_COMBINATORS: Tuple[str, ...] = ("additive", "fused_tier", "eps_band", "off")


def combine_rerank(
    combinator: str,
    fused: Dict[str, float],
    rerank: Dict[str, float],
    rerank_by_class: Dict[str, Dict[str, float]],
    k: int,
    tier_weight: float,
    delta: float,
    limit: int,
) -> Tuple[Dict[str, float], List[str]]:
    """Combine fused RRF scores with the post-fusion rerank classes.

    Args:
        combinator: one of ``VALID_COMBINATORS``.
        fused: ``{gene_id: fused_score}`` — the RRF fused scores, already
            restricted to the eligible id set by the caller.
        rerank: ``{gene_id: total_rerank_additive}`` — the flat sum of all
            rerank-class bonuses (eligible-restricted). Read by ``additive``.
        rerank_by_class: ``{class_name: {gene_id: bonus}}`` — the per-class
            breakdown (eligible-restricted). Read by ``fused_tier``.
        k: RRF constant (``DEFAULT_RRF_K``), used by ``fused_tier``.
        tier_weight: uniform per-class rank post-multiplier (``fused_tier``).
        delta: relative tie-band width δ (``eps_band``).
        limit: return at most this many ranked ids (``limit <= 0`` -> all).

    Returns:
        ``(final_scores, ranked_ids)``. ``final_scores`` maps every eligible
        gene_id to its final score under the combinator (``additive`` adds the
        rerank; ``fused_tier`` adds the rank contributions; ``eps_band`` and
        ``off`` leave it pure fused). ``ranked_ids`` is the emitted ordering,
        truncated to ``limit``.

    Raises:
        ValueError: on an unknown combinator (defensive — the store validates
            at construction, so this only trips on a direct mis-call).
    """
    if combinator == "additive":
        # Byte-identical to the shipped inline block:
        #   final_scores[gid] = fused.get(gid, 0.0) + rerank.get(gid, 0.0)
        final = {g: fused.get(g, 0.0) + rerank.get(g, 0.0) for g in fused}
        return final, _sort_by_score(final, limit)

    if combinator == "off":
        # Floor arm: rerank ignored entirely, pure fused ranking.
        final = dict(fused)
        return final, _sort_by_score(final, limit)

    if combinator == "fused_tier":
        # Each rerank class becomes a rank contribution, exactly like a recall
        # tier: rank its positive-boost members and add tier_weight/(k+rank).
        final = dict(fused)
        for _cls, boosts in rerank_by_class.items():
            positive = [(g, v) for g, v in boosts.items() if v > 0.0]
            for g, rank in rank_by_score(positive):
                final[g] = final.get(g, 0.0) + tier_weight / (k + rank)
        return final, _sort_by_score(final, limit)

    if combinator == "eps_band":
        return _eps_band(fused, rerank, delta, limit)

    raise ValueError(
        "unknown rerank combinator "
        f"{combinator!r}; expected one of {VALID_COMBINATORS}"
    )


def resolve_class_combinator(
    mapping: Dict[str, str], cls: Optional[str]
) -> Optional[str]:
    """Resolve the per-query rerank combinator for a query-classifier class.

    Issue #255 (classifier-gated combinator, default-inert). The stage-0
    rule-based query classifier assigns each query a ``cls``; this maps it to a
    combinator name via the ``[retrieval] rerank_combinator_by_class`` config
    map. Returns the mapped combinator, or ``None`` to mean "fall back to the
    store's global ``rerank_combinator``". Both an empty/absent ``mapping`` and
    a ``None`` ``cls`` (classifier disabled, or no class assigned) resolve to
    ``None`` — the byte-identical default path. Any non-``None`` value returned
    here is already a member of ``VALID_COMBINATORS`` because the map is
    validated at config load (``RetrievalConfig.__post_init__``).
    """
    if not mapping or cls is None:
        return None
    return mapping.get(cls)


def _sort_by_score(scores: Dict[str, float], limit: int) -> List[str]:
    """Sort ids by ``(-score, gene_id)`` — the Fuser's tie-break — and cut."""
    ranked = sorted(scores, key=lambda g: (-scores[g], g))
    return ranked if limit <= 0 else ranked[:limit]


def _eps_band(
    fused: Dict[str, float],
    rerank: Dict[str, float],
    delta: float,
    limit: int,
) -> Tuple[Dict[str, float], List[str]]:
    """ε-band lexicographic tie-break on the fused order.

    Walk the fused-descending order; each not-yet-emitted leader anchors a
    band of the documents whose fused score is within a relative δ of it
    (``fused >= leader * (1 - delta)``). Inside a band, rerank breaks ties
    (``(-rerank, -fused, gene_id)``); a leader with non-positive fused forms a
    singleton band (rerank can never manufacture rank out of a zero-fused
    doc). Final scores stay pure fused — only the emitted order changes.
    """
    final = dict(fused)
    # Base order: fused desc, gene_id asc (the Fuser tie-break).
    base = sorted(fused, key=lambda g: (-fused[g], g))

    ordered: List[str] = []
    i = 0
    n = len(base)
    while i < n:
        leader = base[i]
        leader_score = fused[leader]
        if leader_score <= 0.0:
            # Non-positive leader: singleton band, rerank has nothing to
            # refine. Preserves pure-fused order across the zero tail.
            ordered.append(leader)
            i += 1
            continue
        threshold = leader_score * (1.0 - delta)
        # base is fused-descending, so the band is a contiguous run from i.
        j = i
        while j < n and fused[base[j]] >= threshold:
            j += 1
        band = base[i:j]
        # Within the band, rerank decides; ties fall back to fused then id.
        band.sort(key=lambda g: (-rerank.get(g, 0.0), -fused[g], g))
        ordered.extend(band)
        i = j

    return final, (ordered if limit <= 0 else ordered[:limit])
