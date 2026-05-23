r"""Distribution of /context delivered chars + n_genes for a bench run.

No LLM, no genome -- just reads needles.jsonl. Confirms (or refutes) the
per-gene delivery clamp at scale: if delivered chars cluster near ~1KB*n_genes
regardless of the underlying docs, the expression-time compression is the
ceiling. Reusable to compare control (flat 1000) vs treatment (dynamic budget).

Usage:
  python benchmarks/delivered_chars_dist.py --run benchmarks/results/<run_dir>
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, type=Path)
    args = ap.parse_args()

    chars, ngenes, per_gene, gold = [], [], [], 0
    n = 0
    for line in (args.run / "needles.jsonl").open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        c = d.get("ctx", {}) or {}
        ch = int(c.get("chars", 0) or 0)
        ng = int(c.get("n_genes", 0) or 0)
        chars.append(ch); ngenes.append(ng)
        if ng > 0:
            per_gene.append(ch / ng)
        if c.get("gold_delivered"):
            gold += 1
        n += 1

    def q(xs, p):
        return sorted(xs)[min(len(xs) - 1, int(len(xs) * p))] if xs else 0

    print(f"\n=== {args.run.name} (n={n}) ===")
    print(f"gold_delivered: {gold}/{n}")
    print(f"n_genes        median={statistics.median(ngenes):.0f}  "
          f"mean={statistics.mean(ngenes):.2f}  max={max(ngenes)}")
    print(f"delivered chars  median={statistics.median(chars):.0f}  "
          f"mean={statistics.mean(chars):.0f}  p90={q(chars,0.9)}  max={max(chars)}")
    print(f"chars PER GENE   median={statistics.median(per_gene):.0f}  "
          f"mean={statistics.mean(per_gene):.0f}  p90={q(per_gene,0.9):.0f}  "
          f"max={max(per_gene):.0f}")
    # Histogram of per-gene chars in 250-char buckets up to 2000
    buckets = {}
    for v in per_gene:
        b = min(2000, int(v // 250) * 250)
        buckets[b] = buckets.get(b, 0) + 1
    print("per-gene chars histogram (250-char buckets):")
    for b in sorted(buckets):
        label = f"{b}-{b+249}" if b < 2000 else "2000+"
        print(f"   {label:>10}: {'#' * buckets[b]} ({buckets[b]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
