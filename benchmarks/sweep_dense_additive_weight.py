"""Sweep `dense_additive_weight` across {0.0, 1.0, 2.0, 3.0, 4.0, 6.0} on a
pre-built genome and report per-weight recall + rank shifts.

Issue #138: H10q on the 10K EnterpriseRAG fixture observed sparse-only
(arm 1, dense disabled) recovered +19 pp recall@10 over the H10o-fixed
dense+abs path at the shipped 4.0 weight. This script implements the
weight sweep the issue requested with `0.0` (dense-off arm) as the lower
bound rather than a fallback.

Run against an existing genome -- `dense_additive_weight` is a
QUERY-TIME scoring weight, so no rebuild is needed; the same
`embedding_dense_v2` blobs are scored with a different multiplier per
arm. Useful for tuning without spending ingest budget.

Usage:
    # Default: walks the canonical sweep on the small fixture
    python benchmarks/sweep_dense_additive_weight.py \\
        --genome genomes/bench/matrix/small.db \\
        --queries benchmarks/_dense_weight_sweep_queries.json

    # Minimal smoke (3 queries x 3 weights)
    python benchmarks/sweep_dense_additive_weight.py --smoke

Inputs:
  --genome PATH      Path to a .db built with `splade_enabled=True` and
                     populated `embedding_dense_v2` blobs (anything from
                     `genomes/bench/matrix/` qualifies).
  --queries PATH     JSON file: list of {"query": str, "gold_ids": [str]}.
                     If omitted and --smoke not set, auto-generates from
                     gene tags (synthetic gold = the gene itself).
  --weights LIST     Comma-separated floats; default
                     ``0.0,1.0,2.0,3.0,4.0,6.0``.
  --topk INT         Cut-off for recall@k (default 10).
  --out PATH         Per-arm JSON output path (default stdout).

Output:
  Per-weight row with recall@k, MRR, mean rank-of-gold, and
  ``gold_evicted_count`` (queries where gold appeared in the baseline
  weight=0 arm but fell out of top-k in the higher-weight arm — the
  H10q risk R1 signal).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Allow running directly from the repo root without `pip install -e .`
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_queries(path: str | None, smoke: bool, genome) -> list[dict]:
    """Return list of {"query": str, "gold_ids": [str]}."""
    if path:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            raise SystemExit(f"queries file {path} must be a non-empty JSON list")
        return data

    # Auto-synthesize: pick N random genes, query by their first 6 content
    # tokens, gold = that gene.
    cur = genome.conn.execute(
        "SELECT gene_id, content FROM genes WHERE content IS NOT NULL "
        "AND length(content) >= 40 ORDER BY random() LIMIT ?",
        (3 if smoke else 30,),
    )
    rows = cur.fetchall()
    queries = []
    for r in rows:
        gid = r["gene_id"] if hasattr(r, "keys") else r[0]
        content = r["content"] if hasattr(r, "keys") else r[1]
        toks = content.split()[:8]
        if len(toks) < 4:
            continue
        queries.append({"query": " ".join(toks), "gold_ids": [gid]})
    if not queries:
        raise SystemExit("could not auto-synthesize queries from genome")
    return queries


def _eval_arm(genome, queries: list[dict], topk: int) -> dict:
    """Run all queries against `genome`, return aggregate metrics."""
    n = len(queries)
    hits_at_k = 0
    rr_sum = 0.0
    rank_of_gold = []
    per_query = []
    t0 = time.monotonic()

    for _qi, q in enumerate(queries):
        if _qi and _qi % 25 == 0:
            print(f"[sweep]   ..{_qi}/{n} queries", flush=True)
        query_text = q["query"]
        gold = set(q["gold_ids"])
        # Use query_docs through the high-level path (same path /context uses).
        try:
            docs = genome.query_docs(
                domains=query_text.split(),
                entities=[],
                max_genes=max(topk, 20),
            )
        except Exception as exc:
            per_query.append({"query": query_text, "error": str(exc)})
            continue
        ids = [d.gene_id for d in docs]
        # Rank of first gold hit (1-indexed; 0 = not found)
        rank = 0
        for i, gid in enumerate(ids[:topk], start=1):
            if gid in gold:
                rank = i
                break
        if rank > 0:
            hits_at_k += 1
            rr_sum += 1.0 / rank
            rank_of_gold.append(rank)
        per_query.append({
            "query": query_text, "rank": rank, "topk_ids": ids[:topk],
        })

    elapsed = time.monotonic() - t0
    return {
        "n_queries": n,
        "recall_at_k": hits_at_k / max(n, 1),
        "mrr": rr_sum / max(n, 1),
        "mean_rank_of_gold": (
            sum(rank_of_gold) / len(rank_of_gold) if rank_of_gold else None
        ),
        "wall_s": round(elapsed, 3),
        "per_query": per_query,
    }


def _evicted_count(baseline_arm: dict, current_arm: dict, topk: int) -> int:
    """Queries where baseline arm placed gold in top-k but current arm
    pushed it out (or to rank > topk). Implements the H10q risk R1
    "gold_delivered True->False" signal at the recall layer.
    """
    base_pq = {p["query"]: p for p in baseline_arm.get("per_query", [])}
    cur_pq = {p["query"]: p for p in current_arm.get("per_query", [])}
    evicted = 0
    for q, base in base_pq.items():
        if not base.get("rank") or base["rank"] > topk:
            continue
        cur = cur_pq.get(q)
        if cur is None:
            continue
        if not cur.get("rank") or cur["rank"] > topk:
            evicted += 1
    return evicted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--genome", default="genomes/bench/matrix/small.db",
                        help="Path to pre-built .db genome")
    parser.add_argument("--queries", default=None,
                        help="JSON list of {query, gold_ids}; auto-synth if omitted")
    parser.add_argument("--weights",
                        default="0.0,1.0,2.0,3.0,4.0,6.0",
                        help="Comma-separated float list")
    parser.add_argument("--topk", type=int, default=10)
    parser.add_argument("--smoke", action="store_true",
                        help="3-query, 3-weight smoke run")
    parser.add_argument("--out", default=None,
                        help="JSON output path; stdout if omitted")
    args = parser.parse_args(argv)

    if args.smoke:
        weights = [0.0, 4.0, 8.0]
    else:
        weights = [float(w) for w in args.weights.split(",") if w.strip()]

    if not os.path.exists(args.genome):
        raise SystemExit(f"genome not found: {args.genome}")

    from cymatix_context.genome import Genome

    # Build queries once against the first genome instance.
    bootstrap_g = Genome(path=args.genome, dense_embedding_enabled=False)
    try:
        queries = _load_queries(args.queries, args.smoke, bootstrap_g)
    finally:
        bootstrap_g.close()
    print(f"[sweep] {len(queries)} queries, weights={weights}, topk={args.topk}",
          file=sys.stderr)

    arms = {}
    baseline_arm = None
    for w in weights:
        print(f"[sweep] arm dense_additive_weight={w} ...", file=sys.stderr)
        g = Genome(
            path=args.genome,
            dense_embedding_enabled=True,
            dense_embedding_dim=1024,
            fusion_mode="additive",
            dense_additive_weight=w,
        )
        try:
            arm = _eval_arm(g, queries, args.topk)
        finally:
            g.close()
        if baseline_arm is None:
            baseline_arm = arm
        arm["gold_evicted_vs_baseline"] = _evicted_count(
            baseline_arm, arm, args.topk
        )
        arms[str(w)] = arm
        print(
            f"  recall@{args.topk}={arm['recall_at_k']:.3f} "
            f"mrr={arm['mrr']:.3f} "
            f"mean_rank={arm['mean_rank_of_gold']} "
            f"evicted_vs_w={weights[0]}={arm['gold_evicted_vs_baseline']}",
            file=sys.stderr,
        )

    summary = {
        "genome": args.genome,
        "queries_count": len(queries),
        "topk": args.topk,
        "weights": weights,
        "arms": arms,
    }
    out_text = json.dumps(summary, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out_text)
        print(f"[sweep] wrote {args.out}", file=sys.stderr)
    else:
        print(out_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
