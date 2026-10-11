"""Shared packet-trace chunk construction (issue #493).

Used by BOTH the ``/context`` route (``server/routes_context.py``) and the
ERB ablation ladder (``benchmarks/dogfood/erb/ablation_ladder.py``) so the two
emit byte-identical chunk and verdict shapes. Pure readers: nothing here
mutates the window or the store.
"""

from __future__ import annotations

import types
from typing import Any, Dict, List

from ..schemas import KnowBlock, MissBlock


def verdict_of(kmblock: Any) -> Dict[str, Any]:
    if isinstance(kmblock, KnowBlock):
        return {"kind": "know", "confidence": kmblock.confidence}
    if isinstance(kmblock, MissBlock):
        return {"kind": "miss", "reason": kmblock.reason}
    return {"kind": None}


def fetch_docs_batched(genome: Any, gene_ids: list) -> dict:
    """{gene_id: row} for ids the window did not carry: one SELECT of
    only the two traced columns on read_conn (not N get_doc hydrations).
    Stores without read_conn (sharded adapters) fall back to get_doc."""
    conn = getattr(genome, "read_conn", None)
    if conn is None:
        return {g: d for g in gene_ids
                if (d := genome.get_doc(g)) is not None}
    out = {}
    for i in range(0, len(gene_ids), 400):
        batch = gene_ids[i:i + 400]
        rows = conn.execute(
            "SELECT gene_id, content, source_kind FROM genes WHERE gene_id IN ("
            + ",".join("?" for _ in batch) + ")", batch,
        ).fetchall()
        for r in rows:
            out[r["gene_id"]] = types.SimpleNamespace(
                content=r["content"] or "", source_kind=r["source_kind"])
    return out


def window_timing(window: Any) -> Dict[str, Any]:
    """``build_packet_event`` kwargs (stage_ms / signal_ms / pool) for a
    finished window. Pure reader over request-scoped data the pipeline
    already captured (no new timing). ``pool`` = candidates scored per tier
    (genes carrying a contribution from that tier in the request's
    ``tier_contributions`` snapshot) plus ``pool_total`` (pool reaching
    Tier 5). The per-tier count is computed here, i.e. only when tracing."""
    out: Dict[str, Any] = {}
    stage_ms = getattr(window, "stage_ms", None)
    if stage_ms:
        out["stage_ms"] = dict(stage_ms)
    signal_ms = getattr(window, "signal_ms", None)
    if signal_ms:
        out["signal_ms"] = dict(signal_ms)
    pool: Dict[str, int] = {}
    for contribs in list((getattr(window, "tier_contributions", None) or {}).values()):
        for tier in contribs:
            pool[tier] = pool.get(tier, 0) + 1
    total = getattr(window, "pool_total", None)
    if total is not None:
        pool["pool_total"] = int(total)
    if pool:
        out["pool"] = pool
    return out


def window_chunks(window: Any, genome: Any) -> List[Dict[str, Any]]:
    scores = getattr(window, "retrieval_scores", None) or {}
    contribs = getattr(window, "tier_contributions", None) or {}
    chunks = []
    gids = list(window.expressed_gene_ids or [])
    # Assembly already holds the full docs (window.delivered_docs): no
    # re-read. Anything missing from it (stub/forced windows) comes from
    # ONE batched SELECT on read_conn instead of N get_doc round trips.
    docs = dict(getattr(window, "delivered_docs", None) or {})
    missing = [g for g in gids if g not in docs]
    if missing:
        docs.update(fetch_docs_batched(genome, missing))
    for rank, gid in enumerate(gids, 1):
        doc = docs.get(gid)
        chunks.append({
            "gene_id": gid,
            "text": doc.content if doc is not None else None,
            "rank": rank,
            "score": scores.get(gid),
            "source_kind": getattr(doc, "source_kind", None),
            "lane_contribs": contribs.get(gid) or None,
        })
    return chunks
