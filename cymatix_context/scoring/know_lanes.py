"""Dense-free know/miss confidence from what the store actually produces (#482).

The legacy ``[know]`` logistic (``know_calibration.compute_confidence``) was
fit on 2026-07-06 with dense retrieval on: its second-largest weight is
``lexical_dense_agree``, which cannot fire since dense went default-off, and
its ``s_ref`` is pinned to that era's score scale. Across 30 bench corpora it
ranks right top-1s below wrong ones (pooled AUC 0.41).

``[know] model = "lanes"`` replaces only the confidence number with a
logistic over inputs every query has:

* scale-free score shape: log(top1/top2), gap/top1, log(pool size)
* ``coordinate_confidence`` (path-grain match of the delivered set)
* lane agreement (``lane_signals``): how many independent evidence lanes
  back the fused top-1, whether FTS5's own top-1 is the fused top-1, and how
  much of the query the top-1 text covers. Each enters with a
  ``<name>_missing`` indicator, so an unmeasurable signal is never 0 evidence.

The betas are fit offline (``scripts/fit_know_lanes.py``) on
``know_abstain_replay`` receipts and written into ``[know] lanes_betas``. The
gates before confidence (abstain, freshness, supersession) are unchanged.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

log = logging.getLogger("cymatix.know_lanes")

# Tiers that are independent relevance evidence. Boost tiers (access_rate,
# authority, party_attr, cover_walk) re-weight candidates other lanes found
# and say nothing about whether this query matches them.
EVIDENCE_LANES = frozenset({
    "fts5", "tag_exact", "tag_prefix", "filename_anchor", "pki", "harmonic", "sr", "entity_graph",
    "dense", "splade", "sema_boost", "sema_cold",
})

LANE_KEYS = ("lanes_fired", "top1_lanes", "lanes_top3_agree", "frac_lanes_agree",
             "fts5_top1_is_fused_top1", "query_term_coverage")
BASE_FEATURES = ("log_ratio_top2", "rel_gap", "coordinate_confidence", "log_pool_size")
FEATURE_NAMES = BASE_FEATURES + tuple(n for k in LANE_KEYS for n in (k, f"{k}_missing"))

_TERM = re.compile(r"[a-z0-9]+")
_STOP = frozenset({
    "the", "and", "for", "are", "was", "were", "with", "that", "this", "from", "what", "which",
    "who", "whom", "how", "why", "when", "where", "does", "did", "has", "have", "had", "not",
    "you", "your", "can", "will", "would", "should", "could", "into", "about", "than", "then",
    "there", "their", "they", "them", "its", "our", "any", "all", "but", "out", "use", "using",
})


def _terms(text: str) -> set:
    return {t for t in _TERM.findall((text or "").lower()) if len(t) >= 3 and t not in _STOP}


def lane_signals(
    tier_contributions: Optional[Mapping[str, Mapping[str, float]]],
    fused_order: Sequence[str],
    *,
    query: str,
    top1_text: Optional[str],
    k: int = 3,
) -> Dict[str, Optional[float]]:
    """Agreement + coverage signals for the fused top-1 (None = unmeasurable)."""
    tiers = tier_contributions or {}
    by_lane: Dict[str, List[tuple]] = {}
    for gid, tmap in tiers.items():
        if not isinstance(tmap, Mapping):
            continue
        for lane, score in tmap.items():
            if lane in EVIDENCE_LANES and float(score or 0.0) > 0:
                by_lane.setdefault(lane, []).append((gid, float(score)))
    lanes_fired = len(by_lane)
    top1 = fused_order[0] if fused_order else None

    out: Dict[str, Optional[float]] = {
        "lanes_fired": lanes_fired, "top1_lanes": None, "lanes_top3_agree": None,
        "frac_lanes_agree": None, "fts5_top1_is_fused_top1": None, "query_term_coverage": None,
    }
    if top1 is not None and lanes_fired:
        own_top = {lane: [g for g, _ in sorted(rows, key=lambda r: (-r[1], r[0]))[:k]]
                   for lane, rows in by_lane.items()}
        out["top1_lanes"] = sum(1 for rows in by_lane.values() if any(g == top1 for g, _ in rows))
        agree = sum(1 for top in own_top.values() if top1 in top)
        out["lanes_top3_agree"] = agree
        out["frac_lanes_agree"] = agree / lanes_fired
        if "fts5" in own_top:
            out["fts5_top1_is_fused_top1"] = own_top["fts5"][0] == top1
    q = _terms(query)
    if q and top1_text is not None:
        out["query_term_coverage"] = len(q & _terms(top1_text)) / len(q)
    return out


def score_shape(scores: Mapping[str, float]) -> Dict[str, float]:
    """top/second/gap/ratio/pool and fused order, the served convention.

    Singleton pool: gap and ratio both fall back to top_score (as
    server/helpers.py and know_abstain_replay.py do).
    """
    vals = sorted((float(v) for v in scores.values()), reverse=True)
    top = vals[0] if vals else 0.0
    second = vals[1] if len(vals) > 1 else None
    gap = (top - second) if second is not None else top
    ratio = (top / second) if (second is not None and second > 0) else top
    return {"top_score": top, "score_gap": gap, "ratio_top2": ratio, "pool_size": float(len(vals))}


def fused_order(scores: Mapping[str, float]) -> List[str]:
    return [g for g, _ in sorted(scores.items(), key=lambda kv: (-float(kv[1]), kv[0]))]


def feature_values(row: Mapping) -> Dict[str, float]:
    """Model inputs from a replay row or served values.

    ``row`` keys: top_score, score_gap, ratio_top2, pool_size,
    coordinate_confidence (None -> 0.0), lane_signals (dict, may be None).
    """
    def f(key: str) -> float:
        v = row.get(key)
        return float(v) if v is not None else 0.0

    top, gap = f("top_score"), f("score_gap")
    ratio = f("ratio_top2") or 1.0
    out = {
        "log_ratio_top2": math.log(max(ratio, 1e-9)),
        "rel_gap": (gap / top) if top > 0 else 0.0,
        "coordinate_confidence": f("coordinate_confidence"),
        "log_pool_size": math.log(max(f("pool_size"), 1.0)),
    }
    sig = row.get("lane_signals") or {}
    for key in LANE_KEYS:
        v = sig.get(key)
        out[key] = 0.0 if v is None else float(v)
        out[f"{key}_missing"] = 1.0 if v is None else 0.0
    return out


@dataclass(frozen=True)
class LanesModel:
    """intercept + named betas over FEATURE_NAMES (absent names weigh 0).

    ``platt_a``/``platt_b`` rescale the logit for one store (``z' = a*z + b``,
    fit by ``scripts/calibrate_know_store.py``); the defaults are the identity.
    """
    intercept: float = 0.0
    betas: Mapping[str, float] = field(default_factory=dict)
    platt_a: float = 1.0
    platt_b: float = 0.0

    def confidence(self, features: Mapping[str, float]) -> float:
        z = self.intercept + sum(float(b) * float(features.get(name, 0.0)) for name, b in self.betas.items())
        if self.platt_a != 1.0 or self.platt_b != 0.0:
            z = self.platt_a * z + self.platt_b
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        e = math.exp(z)
        return e / (1.0 + e)


def load_lanes_model(toml_path=None) -> Optional[LanesModel]:
    """The configured lanes model, or None when ``[know] model`` is legacy.

    For callers without a loaded config (the packet builder). Never breaks
    retrieval: an unreadable config means legacy (None).
    """
    try:
        from ..config import load_config

        know = (load_config() if toml_path is None else load_config(str(toml_path))).know
    except Exception:  # noqa: BLE001 -- the loader never breaks retrieval
        log.warning("know_lanes: could not read [know]; using the legacy model", exc_info=True)
        return None
    if getattr(know, "model", "legacy") != "lanes":
        return None
    return LanesModel(float(know.lanes_intercept), dict(know.lanes_betas),
                      float(know.lanes_platt_a), float(know.lanes_platt_b))


def served_lanes_confidence(
    model: LanesModel,
    *,
    scores: Mapping[str, float],
    tier_contributions: Optional[Mapping[str, Mapping[str, float]]],
    query: str,
    top1_text: Optional[str],
    coordinate_confidence: Optional[float],
) -> float:
    """The confidence the lanes model gives one served query."""
    shape = score_shape(scores)
    order = fused_order(scores)
    row = dict(shape, coordinate_confidence=coordinate_confidence,
               lane_signals=lane_signals(tier_contributions, order, query=query, top1_text=top1_text))
    return model.confidence(feature_values(row))


__all__ = [
    "BASE_FEATURES", "EVIDENCE_LANES", "FEATURE_NAMES", "LANE_KEYS", "LanesModel",
    "feature_values", "fused_order", "lane_signals", "load_lanes_model", "score_shape",
    "served_lanes_confidence",
]
