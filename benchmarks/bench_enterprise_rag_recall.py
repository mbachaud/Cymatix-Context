r"""Recall@k sweep for helix on EnterpriseRAG-Bench (retrieval-only, no LLM).

Hits /fingerprint (raw ranked retrieval, score_floor=0) for each question
and records the rank at which the first gold doc appears. Computes
recall@1/@3/@5/@10 + MRR. Requires helix running with
ann_threshold_min_genes >= 10 (so it returns a top-k floor rather than
adaptive-depth).

Usage:
  python benchmarks/bench_enterprise_rag_recall.py --max-questions 100 \
      --types basic,semantic,intra_document_reasoning --k 10
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_enterprise_rag import load_needles, HELIX_URL, _rel_after_sources


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-questions", type=int, default=100)
    ap.add_argument("--types", default="basic,semantic,intra_document_reasoning")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--helix-url", default=HELIX_URL)
    ap.add_argument("--label", default="recall")
    args = ap.parse_args()

    types = [t.strip() for t in args.types.split(",")] if args.types else None
    needles = load_needles(max_questions=args.max_questions, question_types=types)
    print(f"loaded {len(needles)} needles; querying /fingerprint k={args.k}")

    ranks: list[int | None] = []
    rows = []
    for i, n in enumerate(needles, 1):
        gold_rels = {_rel_after_sources(p) for p in n["gold_paths"]}
        gold_rels = {r for r in gold_rels if r}
        try:
            resp = httpx.post(f"{args.helix_url}/fingerprint",
                              json={"query": n["question"],
                                    "max_results": args.k, "score_floor": 0.0},
                              timeout=30)
            fps = resp.json().get("fingerprints", [])
        except Exception as exc:
            print(f"  [{i}] {n['id']} ERROR {exc}")
            ranks.append(None); continue
        # rank of first gold hit
        hit_rank = None
        for fp in fps:
            rel = _rel_after_sources(fp.get("source", "")) or ""
            if rel in gold_rels:
                hit_rank = fp["rank"]
                break
        ranks.append(hit_rank)
        rows.append({"id": n["id"], "type": n["type"], "hit_rank": hit_rank,
                     "n_returned": len(fps), "n_gold": len(gold_rels)})
        if i % 25 == 0:
            print(f"  [{i}/{len(needles)}] ...")

    def recall_at(k: int) -> float:
        hits = sum(1 for r in ranks if r is not None and r < k)
        return hits / len(ranks) * 100 if ranks else 0.0

    mrr = sum(1.0 / (r + 1) for r in ranks if r is not None) / len(ranks) if ranks else 0.0

    print("\n=== RECALL SWEEP RESULTS ===")
    print(f"n_questions: {len(ranks)}")
    print(f"recall@1:  {recall_at(1):.1f}%")
    print(f"recall@3:  {recall_at(3):.1f}%")
    print(f"recall@5:  {recall_at(5):.1f}%")
    print(f"recall@10: {recall_at(10):.1f}%")
    print(f"MRR:       {mrr:.3f}")
    miss = sum(1 for r in ranks if r is None)
    print(f"missed entirely (gold not in top-{args.k}): {miss}/{len(ranks)}")

    out = Path(__file__).resolve().parent / "results" / f"recall_{args.label}_{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "n": len(ranks), "k": args.k,
        "recall@1": recall_at(1), "recall@3": recall_at(3),
        "recall@5": recall_at(5), "recall@10": recall_at(10), "mrr": mrr,
        "missed": miss, "rows": rows,
    }, indent=2), encoding="utf-8")
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
