"""Pure analysis for the per_gene_budget flip-default gates.

``compare_arms`` joins two per-query delivery records (the "fixed" control arm
and the "dynamic" treatment arm) by question id and answers the gate-critical
question: did flipping fixed->dynamic drop any delivered gene, and in
particular did it drop the *gold* gene that the fixed arm delivered?

Delivery records are dicts with at least::

    {"id": str, "delivered_rels": list[str], "gold_delivered": bool}

``delivered_rels`` are the source paths (relative to ``sources/``) of the
``<GENE src="...">`` blocks the /context endpoint actually returned, so a
set-diff between arms is a true delivered-gene diff. The function is pure
(stdlib only) so it unit-tests in isolation; latency percentiles live in the
CLI wrapper, which can lean on numpy.
"""
from __future__ import annotations

from typing import Dict, List


def canon(path: str) -> str:
    """Normalize a source path to its key relative to the corpus ``sources/``.

    Gold paths arrive absolute (``F:\\...\\sources\\linear\\design\\X.json``)
    while delivered ``<GENE src="...">`` values appear either with a
    ``sources/`` prefix (``sources/github/Y.json``) or already relative
    (``linear/design/X.json``). All three must collapse to the same key
    (``linear/design/X.json``). The already-relative form is the one the older
    ``_rel_after_sources`` helper mishandled by returning ``None``, which
    silently zeroed gold matches for every doc whose stored source_id lacked
    the ``sources/`` prefix.
    """
    n = str(path).replace("\\", "/")
    if "/sources/" in n:
        return n.split("/sources/", 1)[1]
    if n.startswith("sources/"):
        return n[len("sources/"):]
    return n


def compare_arms(fixed: List[dict], dynamic: List[dict]) -> Dict:
    """Diff two delivery arms by question id.

    Returns a metrics dict. The gate PASSES iff ``gold_drop_queries`` is empty
    (no question lost its gold gene under dynamic) -- non-gold drops are
    tolerated and even desirable (trimming irrelevant tail genes). ``n`` counts
    only ids present in both arms; ids in exactly one arm are surfaced in
    ``unmatched_ids`` rather than silently scored.
    """
    fixed_by_id = {r["id"]: r for r in fixed}
    dynamic_by_id = {r["id"]: r for r in dynamic}
    common = [qid for qid in fixed_by_id if qid in dynamic_by_id]  # fixed order
    unmatched = sorted(set(fixed_by_id) ^ set(dynamic_by_id))

    total_genes_fixed = 0
    total_genes_dynamic = 0
    queries_with_drop = 0
    queries_with_gain = 0
    gold_delivered_fixed = 0
    gold_delivered_dynamic = 0
    gold_drop_queries: List[str] = []
    gold_gain_queries: List[str] = []
    dropped_gene_examples: List = []

    for qid in common:
        f = fixed_by_id[qid]
        d = dynamic_by_id[qid]
        fset = set(f["delivered_rels"])
        dset = set(d["delivered_rels"])
        total_genes_fixed += len(f["delivered_rels"])
        total_genes_dynamic += len(d["delivered_rels"])

        dropped = fset - dset
        if dropped:
            queries_with_drop += 1
            dropped_gene_examples.append((qid, sorted(dropped)))
        if dset - fset:
            queries_with_gain += 1

        f_gold = bool(f.get("gold_delivered"))
        d_gold = bool(d.get("gold_delivered"))
        gold_delivered_fixed += int(f_gold)
        gold_delivered_dynamic += int(d_gold)
        if f_gold and not d_gold:
            gold_drop_queries.append(qid)
        if d_gold and not f_gold:
            gold_gain_queries.append(qid)

    return {
        "n": len(common),
        "total_genes_fixed": total_genes_fixed,
        "total_genes_dynamic": total_genes_dynamic,
        "queries_with_drop": queries_with_drop,
        "queries_with_gain": queries_with_gain,
        "gold_delivered_fixed": gold_delivered_fixed,
        "gold_delivered_dynamic": gold_delivered_dynamic,
        "gold_drop_queries": gold_drop_queries,
        "gold_gain_queries": gold_gain_queries,
        "dropped_gene_examples": dropped_gene_examples,
        "unmatched_ids": unmatched,
    }
