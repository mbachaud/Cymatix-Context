"""Onyx EnterpriseRAG-Bench recall@10 driver for helix (thread 2).

The long-absent driver (memory erb-onyx-semantic-bench): query -> helix
in-process retrieval -> ranked source_ids -> dsids -> dedupe -> recall@10 by
question_type. Uses `genome.last_query_scores` (full post-fusion ranking, NOT
the budget-truncated expressed window) so recall is measured on retrieval, not
on the expression cap.

Plumbing mirrors benchmarks/located_n1000.py (in-process, read_only,
HELIX_DISABLE_LEARN). Config cells via --set section.key=value.

Cells for the ANN-threshold re-A/B on SEMANTIC queries (PR#250 refuted 0.58->0.47
on LITERAL SIKE; open question is whether it helps where dense carries signal):
  baseline   : (shipped) dense on, thr 0.58, floor 8, rrf
  thr_fix    : --set retrieval.ann_similarity_threshold=0.47
  sigma_fix  : --set retrieval.ann_threshold_sigma_multiplier=2.1
  unmask     : --set retrieval.dense_pool_floor_genes=0   (floor masks the thr)
  dense_off  : --set retrieval.dense_embedding_enabled=false  (isolate dense)

Blobs only (shards relabel fusion to additive; the rrf flip is inert there).

Usage:
  python onyx_recall.py --bed-db <blob.db> --corpus-root F:/tmp/enterprise_rag_10k \
     --questions F:/tmp/ext_ct_helixbench/questions/onyx_500.jsonl \
     --k 10 [--limit N] [--qtype semantic] [--set retrieval.ann_similarity_threshold=0.47]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("HELIX_DISABLE_LEARN", "1")

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from helix_context.config import load_config  # noqa: E402
from helix_context.context_manager import HelixContextManager  # noqa: E402


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def build_dsid_map(corpus_root: str, cache: str) -> dict:
    """{normalized source-file path -> dsid} from each raw file's
    dataset_doc_uuid. Cached (10k files ~ seconds; still worth caching)."""
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    m = {}
    files = glob.glob(os.path.join(corpus_root, "sources", "**", "*.json"), recursive=True)
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        dsid = d.get("dataset_doc_uuid") if isinstance(d, dict) else None
        if dsid:
            m[_norm(fp)] = dsid
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(m, f)
    print(f"  dsid map: {len(m)} files -> {len(set(m.values()))} dsids "
          f"(cache {cache})", file=sys.stderr)
    return m


def load_gene_source_map(bed_db: str) -> dict:
    """{gene_id -> source_id} preloaded once from the bed."""
    c = sqlite3.connect(f"file:{bed_db}?mode=ro", uri=True)
    try:
        return {gid: src for gid, src in
                c.execute("SELECT gene_id, source_id FROM genes")}
    finally:
        c.close()


def _apply_overrides(cfg, overrides):
    for item in overrides:
        path, _, raw = item.partition("=")
        section, _, key = path.strip().partition(".")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw.strip()
        target = getattr(cfg, section, None)
        if target is None or not hasattr(target, key):
            raise SystemExit(f"unknown config field {section}.{key}")
        setattr(target, key, value)


def ranked_dsids(manager, gene_src, dsid_map, query, depth=60):
    """Full post-fusion ranking -> ordered DISTINCT dsids."""
    manager.build_context(query, read_only=True, ignore_delivered=True)
    raw = manager.genome.last_query_scores or {}
    ranked = sorted(raw.items(), key=lambda kv: kv[1], reverse=True)[:depth]
    out, seen = [], set()
    for gid, _ in ranked:
        src = gene_src.get(gid)
        if not src:
            continue
        dsid = dsid_map.get(_norm(src))
        if dsid and dsid not in seen:
            seen.add(dsid)
            out.append(dsid)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bed-db", required=True)
    ap.add_argument("--corpus-root", required=True)
    ap.add_argument("--questions", default="F:/tmp/ext_ct_helixbench/questions/onyx_500.jsonl")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--qtype", default="")
    ap.add_argument("--set", dest="overrides", action="append", default=[])
    ap.add_argument("--label", default="cell")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    _here = _REPO / "benchmarks" / "results"
    _here.mkdir(parents=True, exist_ok=True)
    tag = Path(args.corpus_root).name
    dsid_map = build_dsid_map(args.corpus_root, str(_here / f"dsid_map_{tag}.json"))
    gene_src = load_gene_source_map(args.bed_db)
    print(f"  gene->source: {len(gene_src)} genes", file=sys.stderr)

    cfg = load_config(str(_REPO / "helix.toml"))
    cfg.genome.path = args.bed_db
    _apply_overrides(cfg, args.overrides)
    manager = HelixContextManager(cfg)

    qs = []
    with open(args.questions, encoding="utf-8") as f:
        for line in f:
            q = json.loads(line)
            if not q.get("expected_doc_ids"):
                continue
            if args.qtype and q.get("question_type") != args.qtype:
                continue
            qs.append(q)
    if args.limit:
        qs = qs[:args.limit]

    by_type = defaultdict(lambda: {"n": 0, "hit": 0, "recall_sum": 0.0})
    rows = []
    t0 = time.time()
    for i, q in enumerate(qs):
        gold = set(q["expected_doc_ids"])
        got = ranked_dsids(manager, gene_src, dsid_map, q["question"])
        topk = got[:args.k]
        inter = gold & set(topk)
        hit = 1 if inter else 0
        recall = len(inter) / len(gold)
        qt = q.get("question_type", "?")
        b = by_type[qt]
        b["n"] += 1; b["hit"] += hit; b["recall_sum"] += recall
        rows.append({"qid": q.get("question_id"), "qtype": qt,
                     "hit": hit, "recall": round(recall, 3),
                     "n_gold": len(gold), "gold_rank": [got.index(g) + 1 if g in got else -1 for g in gold]})
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(qs)} ({time.time()-t0:.0f}s)", file=sys.stderr)

    n = len(rows)
    hit = sum(r["hit"] for r in rows)
    rec = sum(r["recall"] for r in rows) / max(1, n)
    print(f"\n=== {args.label} | bed={Path(args.bed_db).name} | overrides={args.overrides} ===")
    print(f"N={n}  hit@{args.k}={hit}/{n}={hit/max(1,n):.3f}  recall@{args.k}={rec:.3f}")
    print(f"{'type':<14} {'n':>4} {'hit@k':>8} {'recall@k':>9}")
    for qt, b in sorted(by_type.items()):
        print(f"{qt:<14} {b['n']:>4} {b['hit']/b['n']:>8.3f} {b['recall_sum']/b['n']:>9.3f}")

    out = args.out or str(_here / f"onyx_recall_{args.label}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"label": args.label, "bed": args.bed_db, "overrides": args.overrides,
                   "k": args.k, "N": n, "hit_at_k": hit / max(1, n), "recall_at_k": rec,
                   "by_type": {qt: {"n": b["n"], "hit_at_k": b["hit"]/b["n"],
                                    "recall_at_k": b["recall_sum"]/b["n"]}
                               for qt, b in by_type.items()},
                   "rows": rows}, f, indent=2)
    print(f"-> {out}")


if __name__ == "__main__":
    main()
