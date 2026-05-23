r"""Diagnose WHERE the recall->correctness gap lives, from existing run artifacts.

For a scored EnterpriseRAG run, joins the per-question score rows (onyx_correct,
trust_class) with the bench needles (gold_delivered, n_genes, chars) and prints
the decisive cross-tab:

    {gold doc delivered?} x {correct / abstain / hallucinate}

The cell "gold delivered BUT not correct" is the chunk-width / delivery-depth /
splice-addressable bucket. The cell "gold NOT delivered" is a ranker-recall
problem that re-chunking will only move via side effects. The ratio between
those two tells us whether a chunk-granularity re-ingest is well-targeted.

No re-ingestion, no helix, no LLM — pure join over JSON artifacts.

Usage:
  python benchmarks/diagnose_correctness_gap.py \
      --run benchmarks/results/enterprise_rag_helix_haiku_20260522T063538Z \
      --score benchmarks/results/enterprise_rag_helix_haiku_20260522T063538Z/onyx_score_10k-depth.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_needles(run_dir: Path) -> dict[str, dict]:
    rows = {}
    with (run_dir / "needles.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rows[d["id"]] = d
    return rows


def load_scores(score_path: Path) -> tuple[dict, dict[str, dict]]:
    data = json.loads(score_path.read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in data.get("rows", [])}
    return data, rows


def outcome_class(score_row: dict) -> str:
    """correct | abstain | hallucination, from the trust+onyx lenses."""
    if score_row.get("onyx_correct"):
        return "correct"
    tc = score_row.get("trust_class")
    if tc == "hallucination":
        return "hallucination"
    # everything else not-correct collapses to abstain (safe-but-unhelpful)
    return "abstain"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, type=Path, help="run dir with needles.jsonl")
    ap.add_argument("--score", required=True, type=Path, help="onyx_score_*.json")
    args = ap.parse_args()

    needles = load_needles(args.run)
    summary, scores = load_scores(args.score)

    # Join on question id.
    cells: dict[tuple[bool, str], list[str]] = {}
    gene_counts_target: list[int] = []   # n_genes for gold-delivered-but-wrong
    char_counts_target: list[int] = []
    single_gene_target = 0               # gold delivered as exactly 1 gene, still wrong

    n_joined = 0
    for qid, srow in scores.items():
        n = needles.get(qid)
        if n is None:
            continue
        n_joined += 1
        ctx = n.get("ctx", {}) or {}
        gold_delivered = bool(ctx.get("gold_delivered", False))
        cls = outcome_class(srow)
        cells.setdefault((gold_delivered, cls), []).append(qid)

        if gold_delivered and cls != "correct":
            ng = int(ctx.get("n_genes", 0) or 0)
            ch = int(ctx.get("chars", 0) or 0)
            gene_counts_target.append(ng)
            char_counts_target.append(ch)
            if ng <= 1:
                single_gene_target += 1

    def cell(g: bool, c: str) -> int:
        return len(cells.get((g, c), []))

    print(f"\n=== {summary.get('label','?')}  (n_joined={n_joined}) ===")
    print(f"onyx correctness: {summary['onyx_lens']['correctness_pct']:.1f}%   "
          f"doc_recall: {summary['onyx_lens']['doc_recall_pct']:.1f}%   "
          f"halluc: {summary['trust_lens']['hallucination_pct']:.1f}%")

    print("\n  gold_delivered |  correct  abstain  halluc  | row_total")
    print("  ---------------+---------------------------- +----------")
    for g in (True, False):
        cor, ab, ha = cell(g, "correct"), cell(g, "abstain"), cell(g, "hallucination")
        print(f"  {'YES' if g else 'NO ':>14} |  {cor:>6}  {ab:>6}  {ha:>6}  | {cor+ab+ha:>6}")
    col_cor = cell(True,"correct")+cell(False,"correct")
    col_ab = cell(True,"abstain")+cell(False,"abstain")
    col_ha = cell(True,"hallucination")+cell(False,"hallucination")
    print("  ---------------+---------------------------- +----------")
    print(f"  {'col_total':>14} |  {col_cor:>6}  {col_ab:>6}  {col_ha:>6}  | {col_cor+col_ab+col_ha:>6}")

    delivered_wrong = cell(True,"abstain")+cell(True,"hallucination")
    not_delivered_wrong = cell(False,"abstain")+cell(False,"hallucination")
    total_wrong = delivered_wrong + not_delivered_wrong

    print("\n  --- the lever question ---")
    print(f"  gold delivered, WRONG  (chunk/depth/splice-addressable) : {delivered_wrong}")
    print(f"  gold NOT delivered     (ranker-recall problem)          : {not_delivered_wrong}")
    if total_wrong:
        print(f"  addressable share of all errors                         : "
              f"{delivered_wrong/total_wrong*100:.0f}%")
    print(f"  correct WITHOUT gold doc (parametric / alt-source)      : {cell(False,'correct')}")

    if gene_counts_target:
        import statistics
        print("\n  --- inside the 'gold delivered but wrong' bucket ---")
        print(f"  n_questions                       : {len(gene_counts_target)}")
        print(f"  delivered as exactly 1 gene       : {single_gene_target}  "
              f"({single_gene_target/len(gene_counts_target)*100:.0f}%)")
        print(f"  n_genes delivered  median / max   : "
              f"{statistics.median(gene_counts_target):.0f} / {max(gene_counts_target)}")
        print(f"  chars delivered    median / max   : "
              f"{statistics.median(char_counts_target):.0f} / {max(char_counts_target)}")
        print("\n  interpretation:")
        print("   - single-gene-but-wrong  => the gold doc's fact is NOT in the one")
        print("     delivered strand: either another strand of the same doc has it")
        print("     (wider chunk fixes) or splice dropped it (lower aggressiveness).")
        print("   - multi-gene-but-wrong   => doc spread across strands, the right")
        print("     strand wasn't ranked/kept: wider chunk or more genes delivered.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
