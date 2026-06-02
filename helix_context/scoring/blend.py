"""
Scoring blend: apply cymatics + harmonic bin + TCM as post-retrieve refiners.

Extracted from ``context_manager.py`` (Sprint refactor, 2026-05).
The logic is byte-identical to the inline ``_apply_candidate_refiners``
method it replaces -- only the calling convention changed (explicit
parameters instead of ``self``).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from ..genome import Genome
    from ..scoring.tcm import TCMSession
    from ..schemas import Gene

log = logging.getLogger(__name__)


def apply_candidate_refiners(
    query: str,
    candidates: List[Gene],
    max_genes: int,
    *,
    genome: Genome,
    cymatics_enabled: bool = True,
    cymatics_peak_width: float = 3.0,
    cymatics_distance_metric: str = "cosine",
    synonym_map: Optional[Dict] = None,
    use_cymatics: bool = True,
    use_harmonic_bin: bool = True,
    use_tcm: bool = True,
    allow_rerank: bool = True,
    rerank_enabled: bool = False,
    ribosome: object = None,
    tcm_session: Optional[TCMSession] = None,
    ray_trace_theta: bool = False,
    theta_weight: float = 1.0,
) -> Tuple[List[Gene], Dict[str, Dict[str, float]]]:
    """Apply post-retrieve candidate refiners before assembly or fingerprinting.

    Returns ``(candidates, refiner_contrib)`` where *refiner_contrib* maps
    gene_id -> {refiner_name: bonus}.
    """
    refiner_contrib: Dict[str, Dict[str, float]] = {}

    if use_cymatics and cymatics_enabled and len(candidates) > 1:
        try:
            from .cymatics import (
                query_spectrum, cached_doc_spectrum,
                flux_score_dispatch, build_weight_vector,
            )
            q_spec = query_spectrum(
                query, synonym_map=synonym_map,
                peak_width=cymatics_peak_width,
            )
            weights = build_weight_vector(
                query, synonym_map=synonym_map,
                peak_width=cymatics_peak_width,
            )
            scores = genome.last_query_scores or {}
            for doc in candidates:
                g_spec = cached_doc_spectrum(doc, peak_width=cymatics_peak_width)
                bonus = flux_score_dispatch(q_spec, g_spec, weights, cymatics_distance_metric) * 0.5
                if bonus:
                    refiner_contrib.setdefault(doc.gene_id, {})["cymatics"] = bonus
                scores[doc.gene_id] = scores.get(doc.gene_id, 0) + bonus
            genome.last_query_scores = scores
            candidates.sort(key=lambda g: scores.get(g.gene_id, 0), reverse=True)
        except Exception:
            log.debug("Cymatics blend failed", exc_info=True)

    if os.environ.get("HELIX_RERANK_DIAG"):
        log.warning(
            "[rerank-diag] pool=%d max_genes=%d allow=%s enabled=%s ribo=%s "
            "has_rerank=%s POOL_env=%r CAP_env=%r",
            len(candidates), max_genes, allow_rerank, rerank_enabled,
            type(ribosome).__name__ if ribosome is not None else None,
            (hasattr(ribosome, "rerank") if ribosome is not None else False),
            os.environ.get("HELIX_RERANK_POOL"), os.environ.get("HELIX_RERANK_CAPTURE"),
        )
    if len(candidates) > max_genes:
        if (
            allow_rerank
            and rerank_enabled
            and ribosome is not None
            and hasattr(ribosome, "rerank")
        ):
            # Experiment capture (HELIX_RERANK_CAPTURE=<path>): record the
            # pre-rerank pool and post-rerank top-k source_ids so the
            # cross-encoder's reordering effect can be scored offline. Default
            # off. See docs/prds/2026-06-02-widened-rerank-experiment.md.
            _cap_path = os.environ.get("HELIX_RERANK_CAPTURE")
            _pre = (
                [getattr(g, "source_id", "") or "" for g in candidates]
                if _cap_path else None
            )
            try:
                candidates = ribosome.rerank(query, candidates, k=max_genes)
            except Exception:
                log.warning("Re-rank failed, falling back to retrieval order", exc_info=True)
                candidates = candidates[:max_genes]
            if _cap_path:
                try:
                    _post = [getattr(g, "source_id", "") or "" for g in candidates]
                    with open(_cap_path, "a", encoding="utf-8") as _fh:
                        _fh.write(json.dumps(
                            {"query": query, "pre": _pre, "post": _post}
                        ) + "\n")
                except Exception:
                    log.warning("rerank capture write FAILED for path %r", _cap_path, exc_info=True)
        else:
            candidates = candidates[:max_genes]

    if use_harmonic_bin and len(candidates) >= 3:
        try:
            from .ray_trace import harmonic_bin_boost
            seed_ids = [g.gene_id for g in candidates[:3]]
            velocity = None
            theta_w = 1.0
            if (
                ray_trace_theta
                and tcm_session is not None
                and tcm_session.depth >= 2
            ):
                velocity = list(tcm_session.context_vector)
                theta_w = theta_weight
            overtones = harmonic_bin_boost(
                seed_ids,
                genome,
                k_rays=100,
                max_bounces=2,
                velocity_vector=velocity,
                theta_weight=theta_w,
            )
            if overtones:
                scores = genome.last_query_scores or {}
                for doc in candidates:
                    if doc.gene_id in overtones:
                        bonus = overtones[doc.gene_id]
                        refiner_contrib.setdefault(doc.gene_id, {})["harmonic_bin"] = bonus
                        scores[doc.gene_id] = scores.get(doc.gene_id, 0) + bonus
                genome.last_query_scores = scores
                candidates.sort(key=lambda g: scores.get(g.gene_id, 0), reverse=True)
        except Exception:
            log.debug("Harmonic bin boost failed", exc_info=True)

    if use_tcm and tcm_session is not None and tcm_session.depth > 0:
        try:
            from .tcm import tcm_bonus
            bonuses = tcm_bonus(tcm_session, candidates, weight=0.3)
            for gid, bonus in bonuses.items():
                if bonus:
                    refiner_contrib.setdefault(gid, {})["tcm"] = bonus
            scores = genome.last_query_scores or {}
            candidates.sort(
                key=lambda g: scores.get(g.gene_id, 0) + bonuses.get(g.gene_id, 0),
                reverse=True,
            )
        except Exception:
            pass

    return candidates, refiner_contrib
