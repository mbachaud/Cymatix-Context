"""diag_coact_reserve_fire.py -- #223 coact-reserve fire-probe.

Drives ``ShardRouter.query_genes`` IN-PROCESS (no server, no HTTP)
against a sharded fixture with ``shard_router._apply_coact_reserve``
instrumented, at ``coact_reserved_slots`` = 0 vs N, mirroring the
``/fingerprint`` ``profile="fast"`` bench path:

    domains, entities = extract_query_signals(query)   # same pure fn
    adapter.query_docs(domains, entities, max_genes=K,
                       use_harmonic=False, use_sr=False, read_only=True)

Per query x arm it records whether the cross-shard promotion pass
produced promoted docs, how many were already inside the internal
``[:2*max_genes]`` cut, how many overflow docs the reserve swapped in
(the FIRE event), and whether the delivered top-``max_genes`` window
differs between arms.

Why not grep the server log for the FIRED line: uvicorn's default
logging config does not emit helix module INFO records, so absence of
the line proves nothing. This probe observes the pure function's
arguments and output directly.

USAGE
-----
python benchmarks/diag_coact_reserve_fire.py \\
    --fixture-main F:/tmp/shard223/medium/main.genome.db \\
    --needles benchmarks/results/shard_gold_medium.jsonl \\
    --needles F:/tmp/shard223/linked_needles.jsonl \\
    --max-genes 10 --reserve 2 \\
    --out docs/research/data/fire_probe.json

No GPU required (adapter dense recall is disabled in V1); runtime is
seconds per hundred needles on the medium fixture.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import helix_context.shard_router as sr
from helix_context.accel import extract_query_signals
from helix_context.sharding import ShardedGenomeAdapter


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture-main", required=True,
                    help="Path to the sharded fixture's main.genome.db.")
    ap.add_argument("--needles", action="append", required=True,
                    help="JSONL needle file(s); repeatable.")
    ap.add_argument("--max-genes", type=int, default=10,
                    help="Delivered-window size K (internal merge cut is 2K).")
    ap.add_argument("--reserve", type=int, default=2,
                    help="coact_reserved_slots value for the treatment arm.")
    ap.add_argument("--out", default=None, help="JSON artifact path.")
    args = ap.parse_args(argv)

    # -- instrument _apply_coact_reserve -------------------------------
    orig_apply = sr._apply_coact_reserve
    calls: list[dict] = []

    def recording_apply(union_ids, promoted, corrected, rrf_all, limit, reserve):
        cut_before = list(union_ids[:limit])
        result = orig_apply(union_ids, promoted, corrected, rrf_all, limit, reserve)
        overflow_used = [g for g in result if g in promoted and g not in cut_before]
        calls.append({
            "n_union": len(union_ids),
            "n_promoted": len(promoted),
            "in_cut": sum(1 for g in cut_before if g in promoted),
            "n_overflow_swapped": len(overflow_used),
            "fired": bool(reserve > 0 and overflow_used),
            "changed_cut": result != cut_before,
        })
        return result

    sr._apply_coact_reserve = recording_apply

    needles: list[dict] = []
    for path in args.needles:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    needles.append(json.loads(line))
    print(f"[fire_probe] {len(needles)} needles, max_genes={args.max_genes}, "
          f"arms: c0 vs c{args.reserve}")

    adapter = ShardedGenomeAdapter(main_path=args.fixture_main)
    arms: dict[str, dict] = {}
    per_arm_top: dict[str, dict] = {}
    try:
        for reserve in (0, args.reserve):
            os.environ["HELIX_SHARD_COACT_RESERVE"] = str(reserve)
            arm = f"c{reserve}"
            stats: Counter = Counter()
            fired_ids: list[str] = []
            per_arm_top[arm] = {}
            for nd in needles:
                q = nd.get("question", "")
                domains, entities = extract_query_signals(q)
                calls.clear()
                try:
                    genes = adapter.query_docs(
                        domains, entities, max_genes=args.max_genes,
                        use_harmonic=False, use_sr=False, read_only=True,
                    )
                except Exception as exc:  # noqa: BLE001 - per-needle isolation
                    stats["query_error"] += 1
                    per_arm_top[arm][nd["id"]] = ["ERROR", repr(exc)]
                    continue
                per_arm_top[arm][nd["id"]] = [
                    g.gene_id for g in genes[: args.max_genes]
                ]
                if not calls:
                    stats["expansion_not_reached"] += 1
                    continue
                stats["expansion_reached"] += 1
                c = calls[-1]
                stats["total_promoted"] += c["n_promoted"]
                stats["total_in_cut"] += c["in_cut"]
                stats["total_overflow_swapped"] += c["n_overflow_swapped"]
                if c["fired"]:
                    stats["fired"] += 1
                    fired_ids.append(nd["id"])
                if c["changed_cut"]:
                    stats["changed_cut"] += 1
            arms[arm] = {"stats": dict(stats), "fired_needle_ids": fired_ids}
            print(f"  {arm}: invoked={stats['expansion_reached']} "
                  f"fired={stats['fired']} swapped={stats['total_overflow_swapped']} "
                  f"in_cut={stats['total_in_cut']} errors={stats['query_error']}")
    finally:
        adapter.close()

    arm_t = f"c{args.reserve}"
    delivered_diffs = [
        nid for nid in per_arm_top["c0"]
        if per_arm_top["c0"][nid] != per_arm_top[arm_t].get(nid)
    ]
    print(f"[fire_probe] delivered top-{args.max_genes} diff c0 vs {arm_t}: "
          f"{len(delivered_diffs)} / {len(per_arm_top['c0'])} unique queries differ")

    if args.out:
        artifact = {
            "benchmark": "diag_coact_reserve_fire",
            "issue": 223,
            "fixture_main": args.fixture_main,
            "needle_files": args.needles,
            "n_needles": len(needles),
            "max_genes": args.max_genes,
            "internal_merge_limit": 2 * args.max_genes,
            "reserve_treatment": args.reserve,
            "arms": arms,
            "delivered_window_diff_count": len(delivered_diffs),
            "delivered_window_diff_ids": delivered_diffs,
            "n_unique_query_ids": len(per_arm_top["c0"]),
            "note": (
                "fired = _apply_coact_reserve swapped promoted docs in from "
                "beyond the internal [:2*max_genes] cut. in_cut counts "
                "promoted docs that survived the flat cut on their own "
                "score (the #223 displacement claim predicts ~0 at "
                "coact_link_boost=0.5)."
            ),
        }
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(f"-> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
