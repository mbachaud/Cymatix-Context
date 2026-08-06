"""run_baseline.py — retrieval baseline for the dogfood bed.

Measures **retrieval only**. No decoder, no consumer ladder, no judge: the
question is whether the pipeline puts a gold gene in front of the caller, not
whether some model then reads it correctly. Mixing the two is how a retrieval
regression hides behind a good model, and how a model regression gets blamed
on retrieval.

Scoring is set membership over gene_ids, taken from
``gene_ids/gold_by_needle.json``. That sidesteps path normalisation entirely —
no substring matching between two machines' directory layouts, which is the
failure mode that produced false negatives on the 2026-07-03 bedsweep.

Reported per needle and in rollup:

* ``delivered`` — a gold gene appears anywhere in the returned set
* ``rank`` — 1-based position of the first gold gene (``None`` if absent).
  Delivery alone is not enough: gold at rank 9 under a budget that seats 5 is
  retrieved-but-unusable, which is exactly the #260 finding.
* ``recall@k`` / ``MRR`` — the rollups comparable across beds

Usage::

    python benchmarks/dogfood/run_baseline.py
    python benchmarks/dogfood/run_baseline.py --k 20 --out benchmarks/dogfood/baseline.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--genome", default="genomes/dogfood/genome.db")
    ap.add_argument("--gold", default="benchmarks/dogfood/gene_ids/gold_by_needle.json")
    ap.add_argument("--resolved", default="benchmarks/dogfood/needles_resolved.json")
    ap.add_argument("--k", type=int, default=12,
                    help="candidates requested per query (default 12 = shipped max_genes_per_turn)")
    ap.add_argument("--out", default="benchmarks/dogfood/baseline.json")
    args = ap.parse_args()

    gold_path = REPO_ROOT / args.gold
    if not gold_path.exists():
        print(f"ERROR: {gold_path} not found — run emit_gene_ids.py first", file=sys.stderr)
        return 2
    gold_by_needle = {k: set(v) for k, v in
                      json.loads(gold_path.read_text(encoding="utf-8")).items()}
    resolved = json.loads((REPO_ROOT / args.resolved).read_text(encoding="utf-8"))

    os.environ["CYMATIX_GENOME_PATH"] = str(REPO_ROOT / args.genome)
    os.environ["CYMATIX_DISABLE_LEARN"] = "1"

    # Go through the real pipeline, not KnowledgeStore directly: the store's
    # constructor defaults disable dense, SPLADE and the rest, so a direct
    # construction would silently measure a stripped-down retriever and call
    # it the baseline. CymatixContextManager threads the shipped config in.
    from cymatix_context.config import load_config  # noqa: E402
    from cymatix_context.context_manager import CymatixContextManager  # noqa: E402

    cfg = load_config()
    cfg.genome.path = str(REPO_ROOT / args.genome)
    manager = CymatixContextManager(cfg)

    results = []
    t0 = time.time()
    for nd in resolved["needles"]:
        name = nd["name"]
        gold = gold_by_needle.get(name, set())
        q0 = time.time()
        try:
            # read_only: never mutate the bed. ignore_delivered: session
            # elision would hide a gene that retrieval did find on turn 1.
            manager.build_context(nd["query"], read_only=True,
                                  ignore_delivered=True, max_genes=args.k)
        except Exception as exc:  # noqa: BLE001 - a failing query is a datapoint
            results.append({"name": name, "error": repr(exc), "delivered": False,
                            "rank": None, "gold_genes": len(gold)})
            continue
        latency_ms = (time.time() - q0) * 1000

        raw = manager.genome.last_query_scores or {}
        ids = [gid for gid, _ in
               sorted(raw.items(), key=lambda kv: kv[1], reverse=True)][:args.k]

        rank = None
        for i, gid in enumerate(ids, start=1):
            if gid in gold:
                rank = i
                break

        results.append({
            "name": name,
            "query": nd["query"],
            "expected": nd["expected"],
            "gold_genes": len(gold),
            "returned": len(ids),
            "delivered": rank is not None,
            "rank": rank,
            "latency_ms": round(latency_ms, 1),
        })

    elapsed = time.time() - t0
    scored = [r for r in results if "error" not in r]
    n = len(scored) or 1
    delivered = [r for r in scored if r["delivered"]]
    ranks = [r["rank"] for r in delivered]

    def recall_at(k: int) -> float:
        return round(sum(1 for r in scored if r["rank"] and r["rank"] <= k) / n, 4)

    rollup = {
        "needles": len(results),
        "errors": len(results) - len(scored),
        "k": args.k,
        f"recall@{args.k}": recall_at(args.k),
        "recall@1": recall_at(1),
        "recall@3": recall_at(3),
        "recall@5": recall_at(5),
        "recall@10": recall_at(10),
        "mrr": round(sum(1 / r for r in ranks) / n, 4) if ranks else 0.0,
        "median_rank_when_delivered": statistics.median(ranks) if ranks else None,
        "median_latency_ms": round(statistics.median(
            [r["latency_ms"] for r in scored if "latency_ms" in r]), 1) if scored else None,
        "seconds_total": round(elapsed, 1),
    }

    payload = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "genome": str(REPO_ROOT / args.genome),
        "scoring": "gene_id set membership (no path matching)",
        "note": "retrieval only — no decoder, no consumer ladder, no judge",
        "rollup": rollup,
        "per_needle": results,
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"needles {rollup['needles']}   errors {rollup['errors']}   k={args.k}\n")
    for r in results:
        if "error" in r:
            print(f"  ERR  {r['name']:38s} {r['error'][:60]}")
            continue
        mark = "ok " if r["delivered"] else "MISS"
        rank = f"r{r['rank']}" if r["rank"] else "  -"
        print(f"  {mark} {r['name']:38s} {rank:>4s}  gold_genes={r['gold_genes']:3d}")
    print(f"\nrecall@1 {rollup['recall@1']}   recall@3 {rollup['recall@3']}   "
          f"recall@5 {rollup['recall@5']}   recall@{args.k} {rollup[f'recall@{args.k}']}")
    print(f"MRR {rollup['mrr']}   median rank (delivered) "
          f"{rollup['median_rank_when_delivered']}   "
          f"median latency {rollup['median_latency_ms']}ms")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
