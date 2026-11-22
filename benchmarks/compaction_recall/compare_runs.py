"""Per-needle diff of two compaction-recall runs (the determinism check).

    python -m benchmarks.compaction_recall.compare_runs RUN_A RUN_B [--arms bm25,cymatix]

Compares needle-set sha256 first (different needles = not comparable), then
every per-needle row of each arm: hit, hedge, delivered tokens, chunk count.
Exit 0 only when every compared row is identical.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _rows(run: Path, arm: str) -> dict:
    out = {}
    for p in sorted((run / "results" / arm).glob("*.jsonl")):
        for r in map(json.loads, p.read_text(encoding="utf-8").splitlines()):
            out[(p.stem, r["cls"], r["key"])] = r
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_a", type=Path)
    ap.add_argument("run_b", type=Path)
    ap.add_argument("--arms", default="bm25,cymatix")
    args = ap.parse_args(argv)

    ra = json.loads((args.run_a / "receipt.json").read_text(encoding="utf-8"))
    rb = json.loads((args.run_b / "receipt.json").read_text(encoding="utf-8"))
    if ra["needle_set_sha256"] != rb["needle_set_sha256"]:
        print("needle sets differ — runs are not comparable")
        return 2
    print(f"needle set {ra['needle_set_sha256'][:12]} identical; "
          f"commits {ra['commit'][:8]} / {rb['commit'][:8]}")

    clean = True
    for arm in [a.strip() for a in args.arms.split(",") if a.strip()]:
        a, b = _rows(args.run_a, arm), _rows(args.run_b, arm)
        keys = sorted(set(a) | set(b))
        diffs = [k for k in keys if a.get(k) != b.get(k)]
        hit_flips = [k for k in keys if (a.get(k) or {}).get("hit") != (b.get(k) or {}).get("hit")]
        print(f"  {arm:8s} rows={len(keys)} identical={len(keys) - len(diffs)} "
              f"differing={len(diffs)} hit_flips={len(hit_flips)}")
        for k in diffs[:5]:
            print(f"     {k}: {a.get(k)} != {b.get(k)}")
        clean = clean and not diffs
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
