"""Measure the dense-admission threshold on query-doc geometry (Phase 1a follow-up).

Phase 1a (docs/research/2026-07-06-phase1a-whitening-ab-results.md)
found the 0/5000 dense-admission failure is a THRESHOLD artifact, not
geometry. This tool pins the exact cause: the shipped
``ann_similarity_threshold`` (mode="absolute") and
``ann_threshold_sigma_multiplier`` (mode="margin_over_random") both gate
QUERY-DOC cosine, but the shipped 0.58 was calibrated on DOC-DOC random
pairs (mean ~0.50) — the BGE-M3 query instruction prefix shifts the
geometry down (query-doc random mean ~0.36), so 0.58 rejects nearly all
golds.

For a labeled set (located_n1000 JSONL: query + planted_gene_id gold)
and a dense-backfilled bed, encodes queries with the SAME BGE-M3 codec
the pipeline uses (task="query"), then reports gold-admission vs
random-FPR across:
  * absolute thresholds (the shipped ann_similarity_threshold path)
  * sigma multipliers k in mu+k*sigma (the margin_over_random path)
  * random-pair percentiles (formula-free alternative)

Offline gold-admission is necessary but NOT sufficient — it does not
model fusion + assembly. Live-A/B before changing a shipped default.

Usage:
    python benchmarks/sweep_ann_sigma.py \
        --bed-db genomes/bench/matrix/xl_clean.db \
        --labels docs/benchmarks/data/2026-07-06-located-n1000-xl-clean-full-rrf.jsonl \
        --device cuda \
        --out benchmarks/results/ann_sigma_sweep_xl_clean.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

_BENCH_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BENCH_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def load_doc_vectors(db_path: str):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT gene_id, embedding_dense_v2 FROM genes "
        "WHERE embedding_dense_v2 IS NOT NULL"
    ).fetchall()
    conn.close()
    ids = [r[0] for r in rows]
    mat = np.frombuffer(b"".join(r[1] for r in rows), dtype="<f4").reshape(
        len(rows), -1
    ).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, mat / norms


def _load_labels(path: str, id_to_idx: dict):
    labels = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                r = json.loads(line)
                if r["planted_gene_id"] in id_to_idx:
                    labels.append((r["query"], r["planted_gene_id"]))
    return labels


def _encode_queries(queries, device, batch=32):
    from helix_context.backends.bgem3_codec import BGEM3Codec

    codec = BGEM3Codec(dim=1024, device=device)
    out = []
    for i in range(0, len(queries), batch):
        out.extend(codec.encode_batch(queries[i:i + batch], task="query"))
    q = np.asarray(out, dtype=np.float32)
    qn = np.linalg.norm(q, axis=1, keepdims=True)
    qn[qn == 0] = 1.0
    return q / qn


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bed-db", default="genomes/bench/matrix/xl_clean.db")
    ap.add_argument("--labels", required=True)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n-random", type=int, default=500_000)
    ap.add_argument("--out", default="benchmarks/results/ann_sigma_sweep.json")
    args = ap.parse_args()

    device = args.device
    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"

    ids, docs = load_doc_vectors(args.bed_db)
    id_to_idx = {g: i for i, g in enumerate(ids)}
    labels = _load_labels(args.labels, id_to_idx)
    if not labels:
        raise SystemExit("no labeled queries with gold in this bed")

    t0 = time.time()
    q = _encode_queries([x[0] for x in labels], device)
    print(f"encoded {len(q)} queries on {device} in {time.time()-t0:.0f}s",
          file=sys.stderr)

    gold_idx = np.array([id_to_idx[g] for _, g in labels])
    gold_sims = np.einsum("ij,ij->i", q, docs[gold_idx])
    rng = np.random.default_rng(args.seed)
    qi = rng.integers(0, len(q), args.n_random)
    di = rng.integers(0, len(docs), args.n_random)
    rand_sims = np.einsum("ij,ij->i", q[qi], docs[di])
    mu, sigma = float(rand_sims.mean()), float(rand_sims.std())

    def row(thr):
        return {
            "threshold": round(float(thr), 4),
            "gold_admit": round(float((gold_sims > thr).mean()), 4),
            "random_fpr": round(float((rand_sims > thr).mean()), 5),
            "avg_false_admits": int(float((rand_sims > thr).mean()) * len(ids)),
        }

    absolute = {f"{t:.2f}": row(t) for t in
                (0.58, 0.52, 0.50, 0.47, 0.44, 0.42, 0.40)}
    sigma_sweep = {f"k={k}": {**row(mu + k * sigma), "k": k}
                   for k in (3.0, 2.5, 2.3, 2.2, 2.0, 1.8)}
    percentile = {f"p{p}": {**row(np.percentile(rand_sims, p)),
                            "random_percentile": p}
                  for p in (95.0, 99.0, 99.5, 99.9)}

    payload = {
        "benchmark": "ann_dense_threshold_sweep",
        "phase": "1a follow-up (J-space council)",
        "bed_db": args.bed_db,
        "labels": args.labels,
        "n_docs": len(ids),
        "n_queries": len(labels),
        "geometry": "query-doc (BGE-M3 task=query vs passage)",
        "random_mean": round(mu, 4),
        "random_std": round(sigma, 4),
        "gold_mean": round(float(gold_sims.mean()), 4),
        "gold_p10": round(float(np.percentile(gold_sims, 10)), 4),
        "shipped_absolute_threshold": 0.58,
        "shipped_sigma_multiplier": 3.0,
        "absolute_thresholds": absolute,
        "sigma_multipliers": sigma_sweep,
        "random_percentiles": percentile,
        "note": "offline gold-admission only; live-A/B before changing a default",
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({
        "random_mean": mu, "gold_mean": float(gold_sims.mean()),
        "shipped_0.58": absolute["0.58"], "candidate_0.47": absolute["0.47"],
    }, indent=2))
    print(f"-> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
