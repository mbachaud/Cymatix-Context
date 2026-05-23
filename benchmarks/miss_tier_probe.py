r"""Tier-contribution diagnostic for the 17 clean retrieval misses (H10j).

For each miss (gold not in top-12 at the recall baseline), deep-retrieve via
/fingerprint (which returns per-result ``tier_contributions``) and answer:

  1. Is gold UNDER-RANKED (surfaced in the deep pool at rank 13..K) or
     NOT SURFACED at all (absent even at depth K)? These are different bugs:
     under-ranked => a tier-weighting problem; absent => candidate-generation
     / indexing / threshold problem.
  2. For under-ranked golds, which tiers credited the gold vs the topical
     rank-1 that beat it? This tests the hypothesis that the entity /
     path_key_index tier under-weights an exact proper-noun match relative to
     dense/lexical topical density.

Reads the miss list from the latest recall_contamination_*.json. Requires helix
running with ann_threshold_max_genes >= K so the deep pool is actually built.

Usage:
  python benchmarks/miss_tier_probe.py --k 50
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_enterprise_rag import load_needles, HELIX_URL
from gate_analysis import canon

RESULTS = Path(__file__).resolve().parent / "results"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=50, help="deep-retrieval depth")
    ap.add_argument("--helix-url", default=HELIX_URL)
    ap.add_argument("--contam", default=None, help="recall_contamination json (default: latest)")
    args = ap.parse_args()

    contam = args.contam or sorted(glob.glob(str(RESULTS / "recall_contamination_*.json")))[-1]
    rows = json.load(open(contam, encoding="utf-8"))["rows"]
    miss_ids = [r["id"] for r in rows if r.get("rank_new") is None]
    print(f"miss list from {Path(contam).name}: {len(miss_ids)} misses")

    needles = {n["id"]: n for n in load_needles(max_questions=None)}

    out_rows = []
    surfaced = absent = 0
    tier_names: set[str] = set()
    with httpx.Client() as client:
        for qid in miss_ids:
            n = needles[qid]
            gold = {c for c in (canon(p) for p in n["gold_paths"]) if c}
            try:
                fps = client.post(
                    f"{args.helix_url}/fingerprint",
                    json={"query": n["question"], "max_results": args.k, "score_floor": 0.0},
                    timeout=45,
                ).json().get("fingerprints", [])
            except Exception as exc:
                out_rows.append({"id": qid, "error": str(exc)}); continue

            gold_rank = gold_score = gold_tiers = None
            for fp in fps:
                if canon(fp.get("source", "")) in gold:
                    gold_rank = fp["rank"]; gold_score = fp["score"]
                    gold_tiers = fp.get("tier_contributions", {})
                    break
            top1 = fps[0] if fps else {}
            for fp in fps:
                tier_names.update((fp.get("tier_contributions") or {}).keys())

            if gold_rank is None:
                absent += 1
            else:
                surfaced += 1
            out_rows.append({
                "id": qid, "type": n["type"], "n_returned": len(fps),
                "gold_rank": gold_rank, "gold_score": gold_score,
                "gold_tiers": gold_tiers,
                "top1_source": canon(top1.get("source", "")),
                "top1_score": top1.get("score"),
                "top1_tiers": top1.get("tier_contributions", {}),
            })

    print(f"\n=== {len(miss_ids)} misses @ deep K={args.k} ===")
    print(f"  UNDER-RANKED (gold surfaced at rank 12..{args.k-1}): {surfaced}")
    print(f"  NOT SURFACED (gold absent even at K={args.k}):       {absent}")
    print(f"  tiers present: {sorted(tier_names)}")

    print("\n--- per-miss detail ---")
    for r in out_rows:
        if r.get("error"):
            print(f"  {r['id']}: ERROR {r['error']}"); continue
        if r["gold_rank"] is None:
            print(f"  {r['id']:<10} NOT-SURFACED (n={r['n_returned']})  top1={r['top1_source']}")
        else:
            gt = {k: v for k, v in (r["gold_tiers"] or {}).items() if v}
            tt = {k: v for k, v in (r["top1_tiers"] or {}).items() if v}
            print(f"  {r['id']:<10} gold@{r['gold_rank']:<2} score={r['gold_score']}  "
                  f"gold_tiers={gt}")
            print(f"  {'':<10}  vs top1 score={r['top1_score']} tiers={tt}")

    # Aggregate: which tiers fire for gold among under-ranked misses?
    fired = collections.Counter()
    for r in out_rows:
        if r.get("gold_rank") is not None:
            for k, v in (r["gold_tiers"] or {}).items():
                if v:
                    fired[k] += 1
    print(f"\n--- tiers crediting gold among {surfaced} under-ranked misses ---")
    for k, c in fired.most_common():
        print(f"  {k:<24} {c}/{surfaced}")

    out = RESULTS / "miss_tier_probe.json"
    out.write_text(json.dumps({"k": args.k, "surfaced": surfaced, "absent": absent,
                               "tiers": sorted(tier_names), "rows": out_rows}, indent=2),
                   encoding="utf-8")
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
