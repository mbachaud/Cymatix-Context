"""BEIR-standard NDCG@10 / recall@k over an ablation-ladder ``--rank-dump`` receipt.

Issue #482. Scores the ranked gene ids the ladder recorded per needle the way
the BEIR evaluator (pytrec_eval / trec_eval ``ndcg_cut``) scores a run:

* document level — the bed's genes are mapped to the emitted document files
  through ``genes.source_id``; several genes of one document count once, at
  the first position;
* graded qrels with LINEAR gains (trec_eval convention), ideal DCG built from
  ALL positive qrels of the query, so gold the bed never ingested (size gate,
  absent from corpus) still costs;
* ``ignore_identical_ids`` — the document whose id equals the query id
  (``self_doc_<tag>.json``) is removed from the ranking before scoring.

Byte-identical documents collapse to one gene at ingest (content-hash dedup),
so a retrieved gene stands for its whole twin group and is credited with the
best grade in the group. That is lenient relative to BEIR, where twins are
distinct ids; the receipt reports how many qrel documents have twins so the
size of the leniency is visible.

Two means are reported: over the needles the ladder ran (resolved), and over
every emitted needle with unresolvable ones scored 0 — the second is the
BEIR-comparable number.

Usage:
  python -P benchmarks/dogfood/beir/beir_ndcg.py --tag beir_scifact \\
      --receipt benchmarks/dogfood/beir_scifact/receipts/ladder_beir_scifact_2026-10-03.json \\
      --db F:/tmp/beir_scifact_bed/beir_scifact.db \\
      --out benchmarks/dogfood/beir_scifact/receipts/beir_ndcg_beir_scifact_2026-10-03.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Set


def ndcg_at_k(ranked_grades: Sequence[int], all_grades: Iterable[int], k: int) -> float:
    """trec_eval-style NDCG@k: linear gains, ideal from every positive qrel."""
    ideal = sorted((g for g in all_grades if g > 0), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    if idcg == 0:
        return 0.0
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(ranked_grades[:k]) if g > 0)
    return dcg / idcg


def collapse_to_docs(ranked_gene_ids: Sequence[str], group_of: Mapping[str, str],
                     exclude: Set[str]) -> List[str]:
    """Ranked genes -> ranked distinct document groups (first position wins)."""
    out, seen = [], set()
    for gid in ranked_gene_ids:
        grp = group_of.get(gid)
        if grp is None or grp in exclude or grp in seen:
            continue
        seen.add(grp)
        out.append(grp)
    return out


def grades_for_docs(groups: Sequence[str], members: Mapping[str, Set[str]],
                    qrels: Mapping[str, int]) -> List[int]:
    """Grade of each ranked group = best qrel grade among its byte-twin members."""
    return [max((qrels.get(p, 0) for p in members.get(g, ())), default=0) for g in groups]


def recall_at_k(groups: Sequence[str], members: Mapping[str, Set[str]],
                qrels: Mapping[str, int], k: int) -> float:
    """Fraction of positive qrel documents covered by the top-k groups."""
    rel = {p for p, s in qrels.items() if s > 0}
    if not rel:
        return 0.0
    hit = set()
    for g in groups[:k]:
        hit |= members.get(g, set()) & rel
    return len(hit) / len(rel)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--bench-dir", help="default benchmarks/dogfood/<tag>")
    ap.add_argument("--corpus-root", help="default from needles_<tag>_meta.json")
    ap.add_argument("--arm", default="baseline")
    ap.add_argument("--basis", choices=("score", "final"), default="score")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    bench = Path(args.bench_dir or f"benchmarks/dogfood/{args.tag}")
    meta = json.loads((bench / f"needles_{args.tag}_meta.json").read_text(encoding="utf-8"))
    root = Path(args.corpus_root or meta["corpus_root"])
    emitted = json.loads((bench / f"needles_{args.tag}.json").read_text(encoding="utf-8"))["needles"]
    grades = json.loads((bench / f"gold_grades_{args.tag}.json").read_text(encoding="utf-8"))
    self_path = bench / f"self_doc_{args.tag}.json"
    self_doc = json.loads(self_path.read_text(encoding="utf-8")) if self_path.exists() else {}

    # Twin groups over the emitted corpus: group id = content sha256.
    group_of_rel: Dict[str, str] = {}
    members: Dict[str, Set[str]] = defaultdict(set)
    for f in root.rglob("*"):
        if f.is_file():
            rel = f.relative_to(root).as_posix()
            sha = _sha256(f)
            group_of_rel[rel] = sha
            members[sha].add(rel)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=120)
    group_of_gene: Dict[str, str] = {}
    unmapped_genes = 0
    try:
        for gid, sid in conn.execute("SELECT gene_id, source_id FROM genes"):
            try:
                rel = Path(sid).relative_to(root).as_posix()
            except (TypeError, ValueError):
                unmapped_genes += 1
                continue
            if rel in group_of_rel:
                group_of_gene[gid] = group_of_rel[rel]
            else:
                unmapped_genes += 1
    finally:
        conn.close()

    receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    arm = next(a for a in receipt["arms"] if a["arm"] == args.arm)
    key = "score_ranked_ids" if args.basis == "score" else "final_ranked_ids"
    by_needle = {r["needle"]: r for r in arm["per_query"]}
    dump_depth = max((len(r.get(key) or []) for r in arm["per_query"]), default=0)
    if dump_depth == 0:
        raise SystemExit(f"receipt has no {key}: run the ladder with --per-query --rank-dump N")

    rows, sums = [], defaultdict(float)
    n_run = 0
    for nd in emitted:
        name = nd["name"]
        qrels = grades.get(name, {})
        rec = by_needle.get(name)
        if rec is None:
            rows.append({"needle": name, "ran": False, "ndcg@10": 0.0})
            continue
        n_run += 1
        excl = {group_of_rel[self_doc[name]]} if name in self_doc and self_doc[name] in group_of_rel else set()
        excl -= {group_of_rel[p] for p in qrels if p in group_of_rel}  # never drop a gold twin
        groups = collapse_to_docs(rec.get(key) or [], group_of_gene, excl)
        ranked = grades_for_docs(groups, members, qrels)
        row = {
            "needle": name, "ran": True,
            "ndcg@10": ndcg_at_k(ranked, qrels.values(), 10),
            "recall@10": recall_at_k(groups, members, qrels, 10),
            "recall@100": recall_at_k(groups, members, qrels, 100),
            "docs_in_dump": len(groups),
            "self_doc_excluded": bool(excl),
        }
        for m in ("ndcg@10", "recall@10", "recall@100"):
            sums[m] += row[m]
        rows.append(row)

    n_all = len(emitted)
    qrel_docs = {p for q in grades.values() for p in q}
    twinned = sum(1 for p in qrel_docs if p in group_of_rel and len(members[group_of_rel[p]]) > 1)
    out = {
        "tool": "benchmarks/dogfood/beir/beir_ndcg.py",
        "tag": args.tag, "receipt": args.receipt, "arm": args.arm, "basis": key,
        "db": args.db, "corpus_root": str(root),
        "rank_dump_depth_genes": dump_depth,
        "recall@100_note": "lower bound when docs_in_dump < 100 (dump depth is in genes)",
        "n_emitted": n_all, "n_ran": n_run,
        "mean_over_ran": {m: round(sums[m] / n_run, 4) if n_run else None
                          for m in ("ndcg@10", "recall@10", "recall@100")},
        "beir_comparable_ndcg@10": round(sums["ndcg@10"] / n_all, 4) if n_all else None,
        "self_doc_excluded_needles": sum(1 for r in rows if r.get("self_doc_excluded")),
        "qrel_docs": len(qrel_docs), "qrel_docs_with_byte_twins": twinned,
        "genes_unmapped_to_corpus": unmapped_genes,
        "per_query": rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"{args.tag}: nDCG@10 {out['beir_comparable_ndcg@10']} (all {n_all}), "
          f"over ran {out['mean_over_ran']} (n={n_run}) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
