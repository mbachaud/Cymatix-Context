r"""Quantify the _rel_after_sources contamination on the recall sweep, and emit
a clean per-query recall dataset (raw /fingerprint sources retained for the
ranker recall-failure taxonomy).

One /fingerprint pass per needle; gold hit-rank computed under BOTH the old
``None``-fallback semantics (``old_rel``) and the fixed pass-through
(``gate_analysis.canon``). If the two recall curves match, the recall sweep was
NOT contaminated (fingerprint sources carry the sources/ prefix); if they
differ, the gap is the false-miss count the bug was hiding.

Requires helix running with ann_threshold_min_genes >= 10 (top-k floor).

Usage:
  python benchmarks/recall_contamination_check.py --max-questions 100 --k 12
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_enterprise_rag import load_needles, HELIX_URL
from gate_analysis import canon


def old_rel(p: str):
    """The pre-fix _rel_after_sources: None when no sources/ marker."""
    n = str(p).replace("\\", "/")
    if "/sources/" in n:
        return n.split("/sources/", 1)[1]
    if n.startswith("sources/"):
        return n[len("sources/"):]
    return None


def recall_at(ranks, k: int) -> float:
    hits = sum(1 for r in ranks if r is not None and r < k)
    return hits / len(ranks) * 100 if ranks else 0.0


def mrr(ranks) -> float:
    return sum(1.0 / (r + 1) for r in ranks if r is not None) / len(ranks) if ranks else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-questions", type=int, default=100)
    ap.add_argument("--types", default="basic,semantic,intra_document_reasoning")
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--helix-url", default=HELIX_URL)
    args = ap.parse_args()

    try:
        h = httpx.get(f"{args.helix_url}/health", timeout=5).json()
        print(f"helix /health: genes={h.get('genes')}")
    except Exception as exc:
        print(f"ERROR: helix unreachable: {exc}", file=sys.stderr)
        return 2

    types = [t.strip() for t in args.types.split(",")] if args.types else None
    needles = load_needles(max_questions=args.max_questions, question_types=types)
    print(f"loaded {len(needles)} needles; /fingerprint k={args.k}")

    ranks_old, ranks_new, rows = [], [], []
    with httpx.Client() as client:
        for i, n in enumerate(needles, 1):
            gold_new = {x for x in (canon(p) for p in n["gold_paths"]) if x}
            gold_old = {x for x in (old_rel(p) for p in n["gold_paths"]) if x}
            try:
                fps = client.post(
                    f"{args.helix_url}/fingerprint",
                    json={"query": n["question"], "max_results": args.k, "score_floor": 0.0},
                    timeout=30,
                ).json().get("fingerprints", [])
            except Exception as exc:
                ranks_old.append(None); ranks_new.append(None)
                rows.append({"id": n["id"], "type": n["type"], "error": str(exc)})
                continue
            srcs = [(fp["rank"], fp.get("source", "")) for fp in fps]
            r_old = next((rk for rk, s in srcs
                          if old_rel(s) is not None and old_rel(s) in gold_old), None)
            r_new = next((rk for rk, s in srcs if canon(s) in gold_new), None)
            ranks_old.append(r_old); ranks_new.append(r_new)
            rows.append({
                "id": n["id"], "type": n["type"],
                "rank_old": r_old, "rank_new": r_new,
                "n_returned": len(srcs),
                "top_sources": [s for _, s in srcs[:5]],
                "gold_canon": sorted(gold_new),
            })
            if i % 25 == 0:
                print(f"  [{i}/{len(needles)}]")

    flips = sum(1 for o, nw in zip(ranks_old, ranks_new) if o is None and nw is not None)
    print("\n=== RECALL: old None-fallback vs fixed pass-through ===")
    for k in (1, 3, 5, 10):
        print(f"  recall@{k:<2}: old={recall_at(ranks_old, k):5.1f}%   new={recall_at(ranks_new, k):5.1f}%")
    print(f"  MRR:       old={mrr(ranks_old):.3f}   new={mrr(ranks_new):.3f}")
    print(f"  false-miss flips recovered by fix (old MISS -> new HIT): {flips}")
    miss_new = [r["id"] for r, nw in zip(rows, ranks_new) if nw is None]
    print(f"  clean misses (gold not in top-{args.k}): {len(miss_new)}/{len(rows)}")
    print(f"  contamination on recall sweep: {'CONFIRMED' if flips else 'REFUTED (sweep was clean)'}")

    out = Path(__file__).resolve().parent / "results" / f"recall_contamination_{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "recall_old": {k: recall_at(ranks_old, k) for k in (1, 3, 5, 10)},
        "recall_new": {k: recall_at(ranks_new, k) for k in (1, 3, 5, 10)},
        "mrr_old": mrr(ranks_old), "mrr_new": mrr(ranks_new),
        "flips": flips, "clean_misses": miss_new, "rows": rows,
    }, indent=2), encoding="utf-8")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
