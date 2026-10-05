"""Dense-free agreement signals for know/miss, computed per query (issue #482).

The shipped [know] logistic's only "two retrievers agree" input is
lexical_dense_agree, which cannot exist with dense retrieval off. These ask
the same question of the lanes the shipped path does run: how many
independent evidence lanes back the fused top-1, and does the top-1 actually
contain the query's terms. Inputs are what the pipeline already publishes per
query (``window.tier_contributions`` / ``genome.last_tier_contributions`` =
{gene_id: {tier: score}}, the fused score-map order) plus the query and the
top-1 document text. Anything that cannot be measured is None, never 0.

Bench-side for now: ``know_abstain_replay.py`` records these per needle and
``know_feature_probe.py`` tests whether they separate hits from misses on
held-out corpora. Nothing here is on the serving path.
"""
from __future__ import annotations

import re
from typing import Dict, List, Mapping, Optional, Sequence

# Tiers that are independent relevance evidence. Boost tiers (access_rate,
# authority, party_attr, cover_walk) re-weight candidates other lanes found
# and say nothing about whether this query matches them.
EVIDENCE_LANES = frozenset({
    "fts5", "tag_exact", "tag_prefix", "filename_anchor", "pki", "harmonic", "sr", "entity_graph",
    "dense", "splade", "sema_boost", "sema_cold",
})

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
