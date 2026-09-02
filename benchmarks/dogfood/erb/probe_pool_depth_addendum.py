"""probe_pool_depth_addendum.py -- ARM 1 addendum.

Three things the main ARM 1 receipt (pool_depth_forensics_2026-09-01.json)
cannot answer from a single (48/50 -> 1000/1000) two-point comparison:

  A. THE ADMISSION-CEILING ARITHMETIC.
     Phase 1 established that a PERFECT re-ranker over the currently observed
     score map caps the bed at (314+47)/470 = 0.768. Recompute that ceiling
     over the DEEP pool: an oracle that can pick any gold present anywhere in
     the 1000-deep admitted pool.

  B. IS fts5_candidate_depth INERT ON ITS OWN?
     The bm25_shortlist post-filter (knowledge_store.py:3830-3868) deletes
     every candidate outside the BM25 top-``bm25_shortlist_size`` of the SAME
     OR-joined term query the probe replays. The probe's raw-BM25 proxy uses
     byte-identical SQL and byte-identical terms, so
     ``bm25_proxy_rank <= bm25_shortlist_size`` is EXACTLY the predicate
     "gold survives the shortlist". Any miss whose gold sits beyond BM25 rank
     50 therefore cannot be admitted by raising fts5_candidate_depth alone,
     at ANY depth. This section counts them. No extra pipeline run needed --
     it is an identity over data the main probe already captured.

  C. LATENCY CURVE OF DEEP CAPTURE.
     Depth is not free and this project was burned once by an undisclosed
     2.5x regression. Sweep the paired knobs over
     {48/50 (shipped), 200, 500, 1000, 2000} on a fixed-seed subsample and
     report p50/p95 wall per depth, plus the marginal gold admitted per
     depth step (where does admission saturate?).

Usage:
    python benchmarks/dogfood/erb/probe_pool_depth_addendum.py --stamp 2026-09-01
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CYMATIX_DISABLE_LEARN", "1")

import probe_pool_depth as P  # noqa: E402  (same directory; reuses the arm runner)

SWEEP_DEPTHS = [0, 200, 500, 1000, 2000]   # 0 == shipped auto (48 fts / 50 shortlist)
SWEEP_N = 25
SWEEP_SEED = 20260901

BASELINE_DELIVERED_GOLD = 314
BASELINE_N = 470
PHASE1_OBSERVED_POOL_CEILING = 0.768  # (314+47)/470


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="2026-09-01")
    ap.add_argument("--main-receipt", default=None)
    ap.add_argument("--skip-sweep", action="store_true")
    args = ap.parse_args(argv)

    main_path = (Path(args.main_receipt) if args.main_receipt
                 else HERE / "receipts"
                 / ("pool_depth_forensics_" + args.stamp + ".json"))
    main_r = json.loads(main_path.read_text(encoding="utf-8"))
    rows = main_r["rows"]
    misses = [r for r in rows if r["cohort"] == "miss"]

    # ── A. ceiling arithmetic ────────────────────────────────────────
    found_deep = sum(1 for r in misses if r["deep_rank"] is not None)
    ceiling_deep = round((BASELINE_DELIVERED_GOLD + found_deep) / BASELINE_N, 4)
    found_bm25 = sum(1 for r in misses if r["bm25_proxy_rank"] is not None)
    ceiling_bm25 = round((BASELINE_DELIVERED_GOLD + found_bm25) / BASELINE_N, 4)

    ceiling = {
        "n_needles": BASELINE_N,
        "baseline_delivered_gold": BASELINE_DELIVERED_GOLD,
        "phase1_observed_pool_ceiling": PHASE1_OBSERVED_POOL_CEILING,
        "phase1_note": ("(314+47)/470 -- a perfect re-ranker over the shipped "
                        "~48-deep observed map"),
        "n_misses": len(misses),
        "misses_with_gold_in_deep_pool": found_deep,
        "oracle_ceiling_over_deep_pool": ceiling_deep,
        "misses_with_gold_in_bm25_20000": found_bm25,
        "oracle_ceiling_over_bm25_20000": ceiling_bm25,
        "headroom_vs_phase1": round(ceiling_deep - PHASE1_OBSERVED_POOL_CEILING, 4),
        "meets_080_target": ceiling_deep >= 0.80,
        "semantics": ("ORACLE ceiling: assumes a perfect selector over the "
                      "admitted pool. It is an UPPER BOUND on what any "
                      "re-ranking or gating work can reach at that admission "
                      "depth, not a prediction of any shipped arm."),
    }

    # ── B. is fts5_candidate_depth inert on its own? ─────────────────
    # bm25_proxy_rank <= 50 is exactly "gold survives the shipped shortlist".
    def split(grp):
        keep = [r for r in grp if r["bm25_proxy_rank"] is not None
                and r["bm25_proxy_rank"] <= 50]
        cut = [r for r in grp if r["bm25_proxy_rank"] is not None
               and r["bm25_proxy_rank"] > 50]
        gone = [r for r in grp if r["bm25_proxy_rank"] is None]
        return {
            "n": len(grp),
            "gold_survives_shortlist_50": len(keep),
            "gold_deleted_by_shortlist_50": len(cut),
            "gold_unreachable_at_bm25_20000": len(gone),
            "fts_depth_alone_could_admit": len(keep),
            "fts_depth_alone_provably_inert_for": len(cut) + len(gone),
        }

    inertness = {
        "identity": ("bm25_proxy_rank uses byte-identical SQL and terms to the "
                     "shipped shortlist query (knowledge_store.py:3848-3851), "
                     "so bm25_proxy_rank <= bm25_shortlist_size IS the "
                     "shortlist-survival predicate. No extra run needed."),
        "all_misses": split(misses),
        "pool_absent": split([r for r in misses if r["bucket"] == "pool_absent"]),
        "near_band": split([r for r in misses if r["bucket"] == "near_band"]),
        "seat_capped": split([r for r in misses if r["bucket"] == "seat_capped"]),
        "conclusion_template": ("A candidate-depth A/B that moves ONLY "
                                "fts5_candidate_depth cannot admit the "
                                "'provably_inert_for' misses at any depth; "
                                "bm25_shortlist_size must move with it."),
    }

    # ── C. latency + admission curve ─────────────────────────────────
    sweep: Dict[str, Any] = {"skipped": True}
    if not args.skip_sweep:
        needles, gold, bucket, miss_names = P.load_inputs()
        rng = random.Random(SWEEP_SEED)
        # Stratify the sweep subsample across the three miss buckets so the
        # admission curve is not dominated by one bucket.
        by_bucket: Dict[str, List[str]] = {}
        for m in miss_names:
            by_bucket.setdefault(bucket.get(m, "?"), []).append(m)
        sub: List[str] = []
        for b in sorted(by_bucket):
            pool = sorted(by_bucket[b])
            take = max(1, round(SWEEP_N * len(pool) / len(miss_names)))
            sub.extend(rng.sample(pool, min(take, len(pool))))
        sub = sorted(sub)[:SWEEP_N]
        print("sweep subsample n=" + str(len(sub)), flush=True)

        per_depth = []
        for d in SWEEP_DEPTHS:
            knobs = ({} if d == 0 else
                     {"retrieval.fts5_candidate_depth": d,
                      "retrieval.bm25_shortlist_size": d})
            res = P.run_arm("depth_" + str(d), knobs, sub, needles, gold, False)
            found = sum(1 for r in res["per_query"]
                        if r["rank_of_first_gold"] is not None)
            per_depth.append({
                "depth": d if d else "shipped(48 fts / 50 shortlist)",
                "knobs": knobs,
                "n": res["n"],
                "wall_ms_p50": res["wall_ms_p50"],
                "wall_ms_p95": res["wall_ms_p95"],
                "wall_ms_mean": res["wall_ms_mean"],
                "map_size_median": res["map_size_median"],
                "gold_found_in_map": found,
                "delivered_gold": sum(r["delivered_gold"] for r in res["per_query"]),
                "errors": res["errors"][:5],
            })
            print("  depth " + str(d) + ": p50=" + str(res["wall_ms_p50"])
                  + "ms found=" + str(found) + "/" + str(res["n"]), flush=True)

        base = per_depth[0]["wall_ms_p50"]
        for e in per_depth:
            e["p50_ratio_vs_shipped"] = (round(e["wall_ms_p50"] / base, 3)
                                         if base else None)
        sweep = {
            "skipped": False,
            "subsample_seed": SWEEP_SEED,
            "subsample_n": len(sub),
            "subsample": sub,
            "subsample_note": ("misses only, stratified by bucket -- an "
                               "admission curve on the population that can "
                               "actually move; NOT a bed-level recall estimate"),
            "per_depth": per_depth,
        }

    receipt = {
        "stamp": args.stamp,
        "probe": "ARM 1 addendum -- ceiling arithmetic, knob inertness, latency curve",
        "tool": "benchmarks/dogfood/erb/probe_pool_depth_addendum.py",
        "tool_sha256": P.sha256_file(Path(__file__)),
        "main_probe_tool_sha256": main_r.get("tool_sha256"),
        "git_sha": P.git_sha(),
        "bed": P.BED,
        "bed_bytes": Path(P.BED).stat().st_size,
        "config": str(P.CONFIG),
        "basis_receipts": [str(main_path)] + main_r.get("basis_receipts", []),
        "kill_criterion": ("No kill of its own -- this addendum is descriptive. "
                           "The ARM 1 kill criterion is carried in "
                           + main_path.name + " and is NOT re-litigated here. "
                           "Section A's ceiling is reported as an ORACLE upper "
                           "bound and must not be quoted as a projected "
                           "delivered_gold_rate."),
        "verdict": main_r.get("verdict"),
        "section_a_ceiling": ceiling,
        "section_b_knob_inertness": inertness,
        "section_c_latency_curve": sweep,
    }

    out = HERE / "receipts" / ("pool_depth_addendum_" + args.stamp + ".json")
    out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(ceiling, indent=2))
    print(json.dumps(inertness["pool_absent"], indent=2))
    print("receipt -> " + str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
