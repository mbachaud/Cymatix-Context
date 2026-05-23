r"""Analyze a fixed-vs-dynamic gate probe pair and print the flip verdict.

Gate 1 (dropped genes): PASS iff no question lost its gold gene under dynamic
  (compare_arms.gold_drop_queries is empty). Non-gold drops are tolerated.
Gate 2 (latency): reports p50/p95 of /context elapsed_s per arm + the delta;
  flags if dynamic p95 regresses sharply (>2x fixed AND >+3s absolute).

Usage:
  python benchmarks/gate_depth_analyze.py \
      --fixed   results/gate_depth_fixed_*.jsonl \
      --dynamic results/gate_depth_dynamic_*.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate_analysis import compare_arms


def _load(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _pct(values: list[float], p: float) -> float:
    return float(np.percentile(values, p)) if values else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixed", required=True)
    ap.add_argument("--dynamic", required=True)
    ap.add_argument("--out", default=None, help="optional JSON summary path")
    args = ap.parse_args()

    fixed_all = _load(args.fixed)
    dynamic_all = _load(args.dynamic)
    fixed_ok = [r for r in fixed_all if r.get("status") == "ok"]
    dynamic_ok = [r for r in dynamic_all if r.get("status") == "ok"]
    n_err = (len(fixed_all) - len(fixed_ok)) + (len(dynamic_all) - len(dynamic_ok))

    cmp = compare_arms(fixed_ok, dynamic_ok)

    f_lat = [r["elapsed_s"] for r in fixed_ok]
    d_lat = [r["elapsed_s"] for r in dynamic_ok]
    f_p50, f_p95 = _pct(f_lat, 50), _pct(f_lat, 95)
    d_p50, d_p95 = _pct(d_lat, 50), _pct(d_lat, 95)
    f_chars = [r["chars"] for r in fixed_ok]
    d_chars = [r["chars"] for r in dynamic_ok]

    gate1_pass = len(cmp["gold_drop_queries"]) == 0
    # Latency regression flag: sharp only if BOTH relative >2x and absolute >3s.
    gate2_regressed = (d_p95 > 2 * f_p95) and (d_p95 - f_p95 > 3.0)

    print("=" * 64)
    print("FLIP-DEFAULT GATES — fixed vs dynamic @ multi-gene depth")
    print("=" * 64)
    print(f"joined questions (ok both arms): {cmp['n']}   non-ok rows: {n_err}")
    if cmp["unmatched_ids"]:
        print(f"  unmatched ids: {cmp['unmatched_ids']}")
    print()
    print("-- Gate 1: dropped genes ------------------------------------")
    print(f"  total genes delivered:   fixed={cmp['total_genes_fixed']}  "
          f"dynamic={cmp['total_genes_dynamic']}")
    print(f"  gold delivered:          fixed={cmp['gold_delivered_fixed']}  "
          f"dynamic={cmp['gold_delivered_dynamic']}")
    print(f"  queries with any drop:   {cmp['queries_with_drop']}  "
          f"(non-gold drops are tolerated)")
    print(f"  queries with any gain:   {cmp['queries_with_gain']}  "
          f"(should be ~0; retrieval is identical)")
    print(f"  GOLD-DROP queries:       {len(cmp['gold_drop_queries'])}  "
          f"{cmp['gold_drop_queries'][:10]}")
    print(f"  gold-gain queries:       {len(cmp['gold_gain_queries'])}  "
          f"{cmp['gold_gain_queries'][:10]}")
    if cmp["dropped_gene_examples"][:5]:
        print("  sample drops (id -> dropped rels):")
        for qid, rels in cmp["dropped_gene_examples"][:5]:
            print(f"    {qid}: {rels}")
    print(f"  => Gate 1 {'PASS' if gate1_pass else 'FAIL'} "
          f"(no gold gene dropped)" if gate1_pass
          else f"  => Gate 1 FAIL — {len(cmp['gold_drop_queries'])} gold genes dropped")
    print()
    print("-- Gate 2: /context latency ---------------------------------")
    print(f"  fixed:   p50={f_p50:.3f}s  p95={f_p95:.3f}s  "
          f"chars median={int(np.median(f_chars)) if f_chars else 0}")
    print(f"  dynamic: p50={d_p50:.3f}s  p95={d_p95:.3f}s  "
          f"chars median={int(np.median(d_chars)) if d_chars else 0}")
    print(f"  delta:   p50={d_p50 - f_p50:+.3f}s  p95={d_p95 - f_p95:+.3f}s")
    print(f"  => Gate 2 {'REGRESSED' if gate2_regressed else 'OK'} "
          f"(sharp = >2x AND >+3s on p95)")
    print()
    verdict = "FLIP OK" if (gate1_pass and not gate2_regressed) else "DO NOT FLIP"
    print(f"VERDICT: {verdict}")
    print("=" * 64)

    if args.out:
        summary = {
            "compare": cmp,
            "latency": {"fixed_p50": f_p50, "fixed_p95": f_p95,
                        "dynamic_p50": d_p50, "dynamic_p95": d_p95},
            "chars_median": {"fixed": float(np.median(f_chars)) if f_chars else 0,
                             "dynamic": float(np.median(d_chars)) if d_chars else 0},
            "gate1_pass": gate1_pass, "gate2_regressed": gate2_regressed,
            "verdict": verdict, "n_err": n_err,
        }
        Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"summary written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
