"""Dense tail-lane gold recovery over a BEIR bed: what does a standalone dense
ranking reach that the head (the shipped pipeline's top-k) misses?

Wider-funnel design (append-only reserved tail seats): the head stays the
pipeline's ranking untouched; a separate dense producer fills N tail seats
with documents not already in the head. This tool scores that design offline:

* head  = the ladder ``--rank-dump`` receipt (baseline arm), document level,
  top ``--head-k`` documents (default 12, the delivery cap);
* dense = brute-force cosine over every gene in the bed, encoded with the
  configured dense codec exactly as dense ingest encodes it
  (``genes.content[:dense_passage_char_cap]``, task="passage"; queries with
  task="query"). The bed is opened read-only; vectors are cached to
  ``--cache`` (an .npz outside the bed) so a rerun skips the encode;
* tail  = dense-ranked documents not in the head, first N for each N.

Per needle and in aggregate it reports: positive qrel docs missed by the head,
how many of those the tail recovers at each N, head+tail recall, dense-alone
NDCG@10/recall, and head NDCG@10. Document grouping, twin leniency and
self-document exclusion reuse ``beir_ndcg.py`` so numbers are comparable.

Usage:
  python -P benchmarks/dogfood/beir/dense_tail_recovery.py --tag beir_scifact \\
      --head-receipt <ladder receipt> --db F:/tmp/beir_scifact_bed/beir_scifact.db \\
      --model google/embeddinggemma-2 --dim 768 --device cuda \\
      --cache F:/tmp/gemma-tail-ab/vec_beir_scifact.npz --out <receipt.json>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beir_ndcg import _sha256, collapse_to_docs, grades_for_docs, ndcg_at_k, recall_at_k  # noqa: E402

TAIL_NS = (4, 8, 16, 32, 64)


def load_gene_texts(db: str, cap: int):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=120)
    try:
        rows = conn.execute("SELECT gene_id, source_id, content FROM genes ORDER BY gene_id").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows], [r[1] for r in rows], [(r[2] or "")[:cap] for r in rows]


def encode_all(codec, texts: List[str], task: str, batch: int) -> np.ndarray:
    out = []
    for i in range(0, len(texts), batch):
        out.extend(codec.encode_batch(texts[i:i + batch], task=task))
        if task == "passage" and (i // batch) % 20 == 0:
            print(f"  encoded {min(i + batch, len(texts))}/{len(texts)} passages", flush=True)
    arr = np.asarray(out, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--head-receipt", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--bench-dir")
    ap.add_argument("--corpus-root")
    ap.add_argument("--arm", default="baseline")
    ap.add_argument("--basis", choices=("score", "final"), default="final")
    ap.add_argument("--head-k", type=int, default=12)
    ap.add_argument("--model", required=True)
    ap.add_argument("--dim", type=int, required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--char-cap", type=int, default=2000,
                    help="must equal [ingestion] dense_passage_char_cap (default 2000)")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--dense-depth", type=int, default=400, help="genes kept per query")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    if Path(args.out).exists():
        raise SystemExit(f"refusing to overwrite existing receipt {args.out}")

    bench = Path(args.bench_dir or f"benchmarks/dogfood/{args.tag}")
    meta = json.loads((bench / f"needles_{args.tag}_meta.json").read_text(encoding="utf-8"))
    root = Path(args.corpus_root or meta["corpus_root"])
    emitted = json.loads((bench / f"needles_{args.tag}.json").read_text(encoding="utf-8"))["needles"]
    grades = json.loads((bench / f"gold_grades_{args.tag}.json").read_text(encoding="utf-8"))
    self_path = bench / f"self_doc_{args.tag}.json"
    self_doc = json.loads(self_path.read_text(encoding="utf-8")) if self_path.exists() else {}

    group_of_rel: Dict[str, str] = {}
    members: Dict[str, Set[str]] = defaultdict(set)
    for f in root.rglob("*"):
        if f.is_file():
            rel = f.relative_to(root).as_posix()
            sha = _sha256(f)
            group_of_rel[rel] = sha
            members[sha].add(rel)

    gene_ids, source_ids, texts = load_gene_texts(args.db, args.char_cap)
    group_of_gene: Dict[str, str] = {}
    for gid, sid in zip(gene_ids, source_ids):
        try:
            rel = Path(sid).relative_to(root).as_posix()
        except (TypeError, ValueError):
            continue
        if rel in group_of_rel:
            group_of_gene[gid] = group_of_rel[rel]

    from cymatix_context.backends.bgem3_codec import BGEM3Codec
    codec = BGEM3Codec(dim=args.dim, device=args.device, model_name=args.model)

    cache = Path(args.cache)
    t0 = time.monotonic()
    if cache.exists():
        z = np.load(cache, allow_pickle=False)
        if list(z["gene_ids"]) != gene_ids or str(z["model"]) != args.model or int(z["dim"]) != args.dim:
            raise SystemExit(f"cache {cache} does not match this bed/model; delete it or pick another path")
        doc_vecs = z["vecs"]
        print(f"loaded {len(gene_ids)} cached passage vectors from {cache}", flush=True)
    else:
        doc_vecs = encode_all(codec, texts, "passage", args.batch)
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache, gene_ids=np.asarray(gene_ids), vecs=doc_vecs,
                 model=np.asarray(args.model), dim=np.asarray(args.dim))
    encode_passages_s = round(time.monotonic() - t0, 1)
    nan_rows = int(np.isnan(doc_vecs).any(axis=1).sum())
    if nan_rows:
        raise SystemExit(f"{nan_rows} passage vectors contain NaN; refusing to score")

    t1 = time.monotonic()
    q_vecs = encode_all(codec, [n["query"] for n in emitted], "query", args.batch)
    encode_queries_s = round(time.monotonic() - t1, 1)
    sims = q_vecs @ doc_vecs.T
    depth = min(args.dense_depth, len(gene_ids))
    top = np.argpartition(-sims, depth - 1, axis=1)[:, :depth]

    receipt = json.loads(Path(args.head_receipt).read_text(encoding="utf-8"))
    arm = next(a for a in receipt["arms"] if a["arm"] == args.arm)
    key = "score_ranked_ids" if args.basis == "score" else "final_ranked_ids"
    by_needle = {r["needle"]: r for r in arm["per_query"]}

    rows = []
    agg = defaultdict(float)
    for qi, nd in enumerate(emitted):
        name = nd["name"]
        qrels = grades.get(name, {})
        rel_docs = {p for p, s in qrels.items() if s > 0}
        excl = {group_of_rel[self_doc[name]]} if name in self_doc and self_doc[name] in group_of_rel else set()
        excl -= {group_of_rel[p] for p in qrels if p in group_of_rel}

        order = top[qi][np.argsort(-sims[qi, top[qi]])]
        dense_groups = collapse_to_docs([gene_ids[j] for j in order], group_of_gene, excl)
        rec = by_needle.get(name)
        head_groups = collapse_to_docs((rec or {}).get(key) or [], group_of_gene, excl)
        head = head_groups[:args.head_k]
        head_set = set(head)

        def covered(groups):
            hit = set()
            for g in groups:
                hit |= members.get(g, set()) & rel_docs
            return hit

        head_hit = covered(head)
        missed = rel_docs - head_hit
        tail_pool = [g for g in dense_groups if g not in head_set]
        row = {
            "needle": name, "head_ran": rec is not None, "n_rel": len(rel_docs),
            "head_hit": len(head_hit), "head_missed": len(missed),
            "head_ndcg@10": ndcg_at_k(grades_for_docs(head_groups, members, qrels), qrels.values(), 10),
            "dense_ndcg@10": ndcg_at_k(grades_for_docs(dense_groups, members, qrels), qrels.values(), 10),
            "dense_recall@12": recall_at_k(dense_groups, members, qrels, 12),
            "dense_recall@100": recall_at_k(dense_groups, members, qrels, 100),
            "dense_first_missed_rank_in_tail": next(
                (i + 1 for i, g in enumerate(tail_pool) if members.get(g, set()) & missed), None),
        }
        for n in TAIL_NS:
            rec_n = len(covered(tail_pool[:n]) & missed)
            row[f"tail{n}_recovered"] = rec_n
            agg[f"tail{n}_recovered"] += rec_n
            agg[f"tail{n}_any"] += 1 if (missed and rec_n) else 0
            agg[f"head+tail{n}_recall"] += (len(head_hit) + rec_n) / len(rel_docs) if rel_docs else 0.0
        agg["rel"] += len(rel_docs)
        agg["head_hit"] += len(head_hit)
        agg["head_missed"] += len(missed)
        agg["needles_with_miss"] += 1 if missed else 0
        agg["head_recall@12"] += len(head_hit) / len(rel_docs) if rel_docs else 0.0
        for m in ("head_ndcg@10", "dense_ndcg@10", "dense_recall@12", "dense_recall@100"):
            agg[m] += row[m]
        rows.append(row)

    n = len(emitted)
    summary = {
        "n_needles": n,
        "qrel_pos_docs": int(agg["rel"]),
        "head_hit_docs": int(agg["head_hit"]),
        "head_missed_docs": int(agg["head_missed"]),
        "needles_with_a_head_miss": int(agg["needles_with_miss"]),
        "head_recall@12_mean": round(agg["head_recall@12"] / n, 4),
        "head_ndcg@10_mean": round(agg["head_ndcg@10"] / n, 4),
        "dense_ndcg@10_mean": round(agg["dense_ndcg@10"] / n, 4),
        "dense_recall@12_mean": round(agg["dense_recall@12"] / n, 4),
        "dense_recall@100_mean": round(agg["dense_recall@100"] / n, 4),
        "tail": {
            str(t): {
                "missed_docs_recovered": int(agg[f"tail{t}_recovered"]),
                "frac_of_missed_recovered": round(agg[f"tail{t}_recovered"] / agg["head_missed"], 4)
                if agg["head_missed"] else None,
                "needles_gaining_a_doc": int(agg[f"tail{t}_any"]),
                "head+tail_recall_mean": round(agg[f"head+tail{t}_recall"] / n, 4),
            } for t in TAIL_NS
        },
    }
    out = {
        "tool": "benchmarks/dogfood/beir/dense_tail_recovery.py",
        "tag": args.tag, "db": args.db, "corpus_root": str(root),
        "head_receipt": args.head_receipt, "head_arm": args.arm, "head_basis": key, "head_k": args.head_k,
        "dense": {"model": args.model, "dim": args.dim, "device": args.device, "char_cap": args.char_cap,
                  "query_prefix_applied": codec._prepare(["Q"], "query")[0][:-1],
                  "passage_prefix_applied": codec._prepare(["P"], "passage")[0][:-1],
                  "genes_encoded": len(gene_ids), "nan_rows": nan_rows, "cache": str(cache),
                  "encode_passages_s": encode_passages_s, "encode_queries_s": encode_queries_s},
        "genes_unmapped_to_corpus": len(gene_ids) - len(group_of_gene),
        "summary": summary,
        "per_query": rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
