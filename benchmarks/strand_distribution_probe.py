r"""Measure strand-count headroom for the chunk-granularity experiment.

Runs CodonChunker over a corpus at several max_chars widths and reports, per
width: total strands, mean/median strands per doc, and the % of docs that are
multi-strand. If most docs are already single-strand at the default 4000, then
"wider chunks" has no headroom and the recall->correctness gap must live in
splice/model, not chunk width. If many docs are 3+ strands, wider chunks can
collapse the fact into the delivered strand.

Mirrors build_enterprise_rag_batched.collect_strands filters exactly
(content_type="code", 50 <= size <= 200000, skip agents.md).

Usage:
  python benchmarks/strand_distribution_probe.py \
      --in-dir F:/tmp/enterprise_rag_10k/sources --widths 4000,8000,16000,40000
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE))

from helix_context.encoding.fragments import CodonChunker

MIN_FILE_SIZE = 50
MAX_FILE_SIZE = 200_000


def iter_docs(in_dir: Path, max_files: int | None):
    n = 0
    for fp in in_dir.rglob("*.json"):
        if fp.name.lower() == "agents.md":
            continue
        try:
            sz = fp.stat().st_size
        except OSError:
            continue
        if sz < MIN_FILE_SIZE or sz > MAX_FILE_SIZE:
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        yield content
        n += 1
        if max_files and n >= max_files:
            break


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-dir", required=True, type=Path)
    ap.add_argument("--widths", default="4000,8000,16000,40000")
    ap.add_argument("--max-files", type=int, default=None)
    args = ap.parse_args()

    widths = [int(w) for w in args.widths.split(",")]

    # Read each doc once; chunk at every width.
    t0 = time.perf_counter()
    docs = list(iter_docs(args.in_dir, args.max_files))
    doc_chars = [len(d) for d in docs]
    print(f"loaded {len(docs)} docs in {time.perf_counter()-t0:.1f}s")
    if not docs:
        print("no docs"); return 1

    print("\n  doc size (chars):  "
          f"median={statistics.median(doc_chars):.0f}  "
          f"mean={statistics.mean(doc_chars):.0f}  "
          f"p90={sorted(doc_chars)[int(len(doc_chars)*0.9)]}  "
          f"max={max(doc_chars)}")

    print("\n  width  | total_strands  mean/doc  median/doc  multi-strand%  >=3-strand%")
    print("  -------+----------------------------------------------------------------")
    for w in widths:
        chunker = CodonChunker(max_chars_per_strand=w)
        counts = []
        for content in docs:
            n_strands = len(chunker.chunk(content, content_type="code"))
            counts.append(n_strands)
        total = sum(counts)
        multi = sum(1 for c in counts if c >= 2) / len(counts) * 100
        three = sum(1 for c in counts if c >= 3) / len(counts) * 100
        print(f"  {w:>6} | {total:>13}  {statistics.mean(counts):>7.2f}  "
              f"{statistics.median(counts):>9.0f}  {multi:>11.1f}%  {three:>9.1f}%")

    print("\n  read: total_strands at 4000 ~ current fixture gene count.")
    print("  multi-strand% = docs where the fact could be in a non-delivered strand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
