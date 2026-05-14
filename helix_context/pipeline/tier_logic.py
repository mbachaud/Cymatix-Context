"""
Dynamic budget tier logic: TIGHT / FOCUSED / BROAD tiering + score floor +
shadow pool + Lagrange pull-back.

Extracted from ``context_manager.py`` (Sprint refactor, 2026-05).
Logic is byte-identical to the inline block it replaces; only the calling
convention changed (explicit parameters instead of ``self``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import AbstainClassFloors
    from ..schemas import Gene

log = logging.getLogger(__name__)


@dataclass
class TierResult:
    """Return value from :func:`apply_budget_tiers`.

    ``abstain`` is ``True`` when the ABSTAIN gate fired (weak retrieval on
    both floor and ratio). The caller should build and return the abstain
    ContextWindow without further pipeline steps.
    """
    candidates: List[Gene]
    budget_tier: str = "broad"
    budget_tokens_est: int = 15000
    shadow_pool: List[Gene] = field(default_factory=list)
    shadow_scores: Dict[str, float] = field(default_factory=dict)
    # ABSTAIN sentinel fields
    abstain: bool = False
    abstain_top_score: float = 0.0
    abstain_ratio: float = 0.0


def apply_budget_tiers(
    candidates: List[Gene],
    all_scores: Optional[Dict[str, float]],
    cls_floors: AbstainClassFloors,
    *,
    abstain_enabled: bool = True,
    fusion_mode: str = "additive",
) -> TierResult:
    """Apply TIGHT / FOCUSED / BROAD tiering + score floor + shadow pool.

    Dynamic budget tiers -- size the retrieval window based on
    retrieval confidence instead of always sending max_genes.

    The insight: on a CURATED query ("what port does helix use?") the
    top document will score 5-10x higher than #12. Sending 12 documents for a
    query with an obvious winner wastes 91% of the budget on padding
    and dilutes the small model's attention.

    Tiers (confidence = top_score / mean_score ratio):
      - TIGHT   (ratio >= 3.0): top 3 documents   -- ~6K total tokens
      - FOCUSED (ratio 1.8-3.0): top 6 documents  -- ~9K total tokens
      - BROAD   (ratio < 1.8):  top max_genes     -- ~15K total tokens

    Score-gate floor: always drop documents scoring < 15% of top score.

    Returns a :class:`TierResult` with the trimmed candidates, tier name,
    budget estimate, and shadow pool + scores. When the ABSTAIN gate fires,
    ``result.abstain`` is ``True`` and the caller should build the abstain
    ContextWindow.
    """
    result = TierResult(candidates=list(candidates))

    if len(candidates) <= 3:
        return result

    # Compute ratio over CANDIDATES only, not all scored documents
    # (all_scores includes documents that didn't make top-N cut,
    # dragging down mean and inflating ratio -> always "tight")
    candidate_ids = {g.gene_id for g in candidates}
    scores = {gid: s for gid, s in (all_scores or {}).items() if gid in candidate_ids}
    if not scores or not any(scores.values()):
        return result

    top_score = max(scores.values())
    mean_score = sum(scores.values()) / len(scores) if scores else 1.0
    ratio = top_score / max(mean_score, 0.01)

    # Hard floor: drop anything below 15% of top
    # Shadow scores: preserve cut documents' scores with 0.5x weight
    # so Lagrange check and harmonic binning can pull them back
    # if the landscape changes downstream.
    floor = top_score * 0.15
    gated = [g for g in candidates if scores.get(g.gene_id, 0) >= floor]
    shadow_pool: List[Gene] = [g for g in candidates if scores.get(g.gene_id, 0) < floor]
    if len(gated) >= 3:
        candidates = gated

    # -- Stage 3 transitional bypass (spec S9) --
    # Under RRF, the score scale collapses to ~Sweight/(k+1) ~ 0.3
    # max -- the absolute TIGHT/FOCUSED floors are calibrated for
    # the additive scale and would force every query to BROAD.
    # Stage 4 owns the recalibrated floors. Until then, RRF mode
    # operates on ratio gates only.
    #
    # Issue #115: also gates the ABSTAIN absolute-floor clause below.
    # ``ShardedGenomeAdapter`` declares ``_fusion_mode = "rrf"`` so the
    # router's RRF-fused scores (~0.26-0.40) don't trip the BM25-calibrated
    # 2.5 floor on every sharded query.
    skip_absolute_floors = (fusion_mode == "rrf")

    # -- ABSTAIN gate --------------------------------------------------------
    # When retrieval is weak on BOTH the absolute floor AND the ratio,
    # inject a marker-only ContextWindow so the small model answers from
    # weights instead of digesting 12K of irrelevant noise. Reuses the
    # existing FOCUSED_SCORE_FLOOR (defined just below) verbatim -- strict
    # < on both axes. Telemetry fires here before the early-return so
    # tier="abstain" lands on budget_tier_counter alongside the other
    # tier counts emitted by the existing call site below.
    #
    # Stage 4 (2026-05-08): when [abstain].mode='per_classifier', use
    # the calibrated abstain_top for this query's class instead of
    # the hard-coded 2.5. mode='global' (default) preserves the
    # legacy constant byte-for-byte. ``cls_for_floors`` is hoisted
    # above (set from classifier_result so all branches see it).
    #
    # Under RRF (skip_absolute_floors=True), the absolute-score clause is
    # bypassed; ratio<1.8 alone gates abstain. This mirrors the TIGHT /
    # FOCUSED bypass below — same flag, same rationale.
    FOCUSED_SCORE_FLOOR_FOR_ABSTAIN = cls_floors.abstain_top
    if (
        abstain_enabled
        and (skip_absolute_floors or top_score < FOCUSED_SCORE_FLOOR_FOR_ABSTAIN)
        and ratio < 1.8
    ):
        try:
            from ..telemetry import budget_tier_counter
            budget_tier_counter().add(1, attributes={"tier": "abstain"})
        except Exception:  # pragma: no cover
            pass
        result.abstain = True
        result.abstain_top_score = top_score
        result.abstain_ratio = ratio
        return result

    # Confidence tiering (with shadow pool tracking)
    #
    # Absolute floors prevent the ratio from triggering TIGHT/FOCUSED
    # when ALL candidates are weak. Before the floor, a query with
    # top_score=1.2, mean=0.4 (ratio=3.0) got the same "tight" treatment
    # as top=8.5, mean=2.8 -- even though the first is "retrieval is
    # uncertain, widen the net" and the second is "we found it, send 3."
    # Empirically: on N=50 KV-harvest bench (2026-04-12), 45/50 failed
    # queries landed in tight mode with top_score < 3.0. Adding the
    # absolute floor keeps weak-signal queries in BROAD mode where
    # the larger candidate set gives them a recall chance.
    # Stage 4 (2026-05-08): per-classifier tight/focused floors.
    # mode='global' (default) keeps the legacy 5.0 / 2.5 constants
    # exactly. mode='per_classifier' substitutes the calibrated
    # tight_top / focused_top for this query's class.
    TIGHT_SCORE_FLOOR = cls_floors.tight_top
    FOCUSED_SCORE_FLOOR = cls_floors.focused_top
    # skip_absolute_floors hoisted above the ABSTAIN gate (issue #115).

    budget_tier = "broad"
    budget_tokens_est = 15000

    if (
        ratio >= 3.0
        and (skip_absolute_floors or top_score >= TIGHT_SCORE_FLOOR)
        and len(candidates) >= 3
    ):
        # High confidence -- top document dominates AND is strong, send 3
        shadow_pool = shadow_pool + candidates[3:]
        candidates = candidates[:3]
        budget_tier = "tight"
        budget_tokens_est = 6000
    elif (
        ratio >= 1.8
        and (skip_absolute_floors or top_score >= FOCUSED_SCORE_FLOOR)
        and len(candidates) >= 6
    ):
        # Moderate confidence -- narrow to 6
        shadow_pool = shadow_pool + candidates[6:]
        candidates = candidates[:6]
        budget_tier = "focused"
        budget_tokens_est = 9000
    # else: broad -- keep current up-to-max_genes set
    #   (weak absolute scores or weak ratio -> widen the net)

    shadow_scores = {
        g.gene_id: scores.get(g.gene_id, 0) * 0.5
        for g in shadow_pool
    }

    log.debug(
        "Dynamic budget: tier=%s ratio=%.2f top=%.1f mean=%.1f genes=%d shadow=%d",
        budget_tier, ratio, top_score, mean_score, len(candidates), len(shadow_pool),
    )

    # Telemetry: budget-tier distribution over queries.
    try:
        from ..telemetry import budget_tier_counter
        budget_tier_counter().add(
            1, attributes={"tier": budget_tier},
        )
    except Exception:  # pragma: no cover
        pass

    # Lagrange point check: a document in the shadow pool with HIGH
    # standalone score but LOW co-activation with the winners is
    # being deflected by cluster gravity, not rejected on merit.
    # Pull it back if its standalone > 70% of winners' floor AND
    # its co-activation overlap with winners is < 20%.
    if shadow_pool and len(candidates) >= 3 and budget_tier != "broad":
        try:
            winner_ids = {g.gene_id for g in candidates}
            winner_coact: set[str] = set()
            for g in candidates:
                winner_coact.update(g.epigenetics.co_activated_with or [])
            winner_floor = min(scores.get(g.gene_id, 0) for g in candidates)
            lagrange_threshold = winner_floor * 0.7

            # Rank shadow pool by standalone score
            shadow_ranked = sorted(
                shadow_pool,
                key=lambda g: shadow_scores.get(g.gene_id, 0),
                reverse=True,
            )
            for g in shadow_ranked[:3]:  # check top 3 shadow candidates
                shadow_score = scores.get(g.gene_id, 0)
                if shadow_score < lagrange_threshold:
                    break  # standalone too weak
                # Co-activation overlap with winners
                g_coact = set(g.epigenetics.co_activated_with or [])
                overlap = len(g_coact & (winner_ids | winner_coact))
                overlap_ratio = overlap / max(len(g_coact), 1) if g_coact else 1.0
                if overlap_ratio < 0.2:
                    # Low co-activation with winners -> being deflected
                    log.debug(
                        "Lagrange pull-back: gene %s (score=%.2f, overlap=%.1f%%)",
                        g.gene_id[:12], shadow_score, overlap_ratio * 100,
                    )
                    # Replace the weakest winner with this document
                    candidates[-1] = g
                    break
        except Exception:
            # Lagrange check is a bonus, never blocks -- but log
            # so failures don't silently disable the tier.
            log.warning("Lagrange pull-back failed", exc_info=True)

    result.candidates = candidates
    result.budget_tier = budget_tier
    result.budget_tokens_est = budget_tokens_est
    result.shadow_pool = shadow_pool
    result.shadow_scores = shadow_scores
    return result
