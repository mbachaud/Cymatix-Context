"""COVER-edge walk — W2.1 semantic ranking signal (2026-08-28).

Wave-2 grounding (docs/research/2026-08-28-wave2-semantic-ranking-graph-
research.md) found the one populated gene-gene substrate on bulk-ingested
beds is ``gene_relations`` COVER (relation=5): an entity-overlap kNN written
at ingest by ``storage/co_activation.auto_link_by_entity`` (8.5M edges on
the 947k v09x bed) with, until now, zero query-time consumers. The
reachability pre-flight (receipt
``w2_cover_reachability_preflight_2026-08-28.json``) measured 72% of
static-cohort semantic gold within 2 hops of the retrieval head, 16/61 at
one hop inside a median frontier of ~960.

This module is the walk itself: a confidence-weighted, gamma-damped,
degree-capped mass spread from the fused head, modeled on the SR walker
(``retrieval/sr.py``) but over COVER edges and returning raw mass (the
caller decides how the mass enters ranking — eps-band-confined class or
append-below-head, never a free additive; wave-1 lesson 4).

Direction: bidirectional when the reverse adjacency index
(``idx_gene_relations_rel_b``, storage/ddl.py) exists; forward-only
otherwise — an in-edge lookup without that index is a full-table scan on
multi-GB beds, so the walk degrades rather than stalls.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence, Tuple, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..genome import Genome

log = logging.getLogger("cymatix.cover_walk")

_BATCH = 500  # ids per IN(...) round-trip, matches the store's batching habit


def cover_walk_mass(
    genome: "Genome",
    seed_ids: Sequence[str],
    *,
    hops: int = 2,
    gamma: float = 0.5,
    degree_cap: int = 1000,
    frontier_cap: int = 2000,
) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Spread mass from ``seed_ids`` over the COVER graph.

    Returns ``(mass, diag)``: ``mass`` maps every reached NON-seed gene_id
    to its accumulated gamma-discounted mass; ``diag`` carries
    ``{"mode": "bidirectional"|"forward_only"|"absent"|"error",
    "frontier": [per-hop wavefront sizes], "seeds_in_graph": int}``.

    Semantics: uniform seed mass ``1/len(seeds)``; a node's outgoing share
    is ``gamma * mass * conf_i / sum(conf)`` over its edges (mass-conserving,
    confidence-weighted); a node whose degree exceeds ``degree_cap`` is
    never EXPANDED (hub guard — it can still receive mass); the wavefront
    is truncated to the top ``frontier_cap`` nodes by ``(-mass, gene_id)``
    each hop. Soft-fails: never raises out of retrieval.
    """
    diag: Dict[str, Any] = {"mode": "absent", "frontier": [], "seeds_in_graph": 0}
    if not seed_ids:
        return {}, diag
    try:
        cur = genome.read_conn.cursor()
        has_table = cur.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='gene_relations'"
        ).fetchone()[0]
        if not has_table:
            return {}, diag
        have_reverse = any(
            row[1] == "idx_gene_relations_rel_b"
            for row in cur.execute("PRAGMA index_list('gene_relations')")
        )
        diag["mode"] = "bidirectional" if have_reverse else "forward_only"

        # adjacency cache: gid -> {neighbor: confidence}. A node that
        # accumulates more than degree_cap neighbors is flagged hub and its
        # neighbor map is dropped (it will receive mass but never expand).
        adj: Dict[str, Dict[str, float]] = {}
        hubs: set = set()

        def _fill(gids: List[str]) -> None:
            missing = [g for g in gids if g not in adj and g not in hubs]
            for i in range(0, len(missing), _BATCH):
                batch = missing[i:i + _BATCH]
                ph = ",".join("?" * len(batch))
                for g in batch:
                    adj.setdefault(g, {})
                directions = [
                    (f"SELECT gene_id_a, gene_id_b, confidence FROM gene_relations "
                     f"WHERE relation=5 AND gene_id_a IN ({ph})", 0),
                ]
                if have_reverse:
                    directions.append(
                        (f"SELECT gene_id_a, gene_id_b, confidence FROM gene_relations "
                         f"WHERE relation=5 AND gene_id_b IN ({ph})", 1),
                    )
                for sql, rev in directions:
                    for a, b, conf in cur.execute(sql, batch):
                        node, nb = (b, a) if rev else (a, b)
                        if node in hubs:
                            continue
                        nmap = adj.get(node)
                        if nmap is None:
                            continue
                        prev = nmap.get(nb)
                        if prev is None or conf > prev:
                            nmap[nb] = float(conf)
                        if len(nmap) > degree_cap:
                            hubs.add(node)
                            del adj[node]

        seed_list = list(dict.fromkeys(seed_ids))
        seed_set = set(seed_list)
        seed_mass = 1.0 / len(seed_list)
        mass: Dict[str, float] = {g: seed_mass for g in seed_list}
        accumulated: Dict[str, float] = {}

        _fill(seed_list)
        diag["seeds_in_graph"] = sum(
            1 for g in seed_list if adj.get(g) or g in hubs
        )

        for _hop in range(hops):
            if len(mass) > frontier_cap:
                top = sorted(mass.items(), key=lambda x: (-x[1], x[0]))[:frontier_cap]
                mass = dict(top)
            _fill(list(mass.keys()))
            next_mass: Dict[str, float] = {}
            for gid, m in mass.items():
                neigh = adj.get(gid)
                if not neigh:  # hub, leaf, or unknown node — never expanded
                    continue
                total_conf = sum(neigh.values())
                if total_conf <= 0.0:
                    continue
                scale = (gamma * m) / total_conf
                for nb, conf in neigh.items():
                    next_mass[nb] = next_mass.get(nb, 0.0) + scale * conf
            if not next_mass:
                break
            diag["frontier"].append(len(next_mass))
            for nb, m in next_mass.items():
                accumulated[nb] = accumulated.get(nb, 0.0) + m
            mass = next_mass

        return (
            {g: v for g, v in accumulated.items() if g not in seed_set},
            diag,
        )
    except Exception:
        log.warning("cover walk failed; returning empty mass", exc_info=True)
        diag["mode"] = "error"
        return {}, diag
