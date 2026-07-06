"""Phase 1a — global Ledoit-Wolf whitening A/B vs raw cosine (measured).

J-space roadmap council move #4 (docs/councils/2026-07-06-jspace-roadmap
-council.md): *"Phase 1a: one global Ledoit-Wolf whitening over
`embedding_dense_v2`, A/B'd (measured, not assumed) against cosine on
the now-committed harness."* The council's verified anisotropy evidence:
the margin-over-random calibrated ANN threshold (0.779) sat ABOVE the
corpus max query-doc cosine (0.713) → 0/5000 dense admissions
(knowledge_store.py:383-404 comment block; dense_pool_floor_genes #214
was the band-aid). BGE-M3 vectors are L2-normalized, so whitening's
effect must be measured, not assumed.

Design — offline dense-only A/B on a fully-backfilled bed:

  arm A (baseline): cosine over raw L2-normalized `embedding_dense_v2`
  arm B (whitened): center on the corpus mean, apply W = Σ^(-1/2) from a
      Ledoit-Wolf shrinkage covariance fit on the DOC vectors only (no
      query leakage), re-L2-normalize, cosine.

Labeled queries come from the located_n1000 JSONL (same bed → each row's
`planted_gene_id` is the gold document). Query encoding uses the same
BGE-M3 codec as the pipeline (`task="query"` instruction prefix,
Matryoshka truncate + renormalize) so arm A reproduces the shipped dense
tier's geometry exactly.

Reported per arm:
  * retrieval@1/@5/@10, MRR (dense-only ranking over the whole bed)
  * gold-pair vs random-pair similarity distributions (mean/p50/p90)
    and their rank-based AUC — the separation the ANN threshold needs
  * margin-over-random threshold (mu + 3*sigma of random pairs, the
    Stage-4 formula) and the fraction of gold pairs clearing it — the
    direct re-test of the 0/5000 admission failure

Usage:

    python benchmarks/ab_whitening_dense.py \
        --bed-db genomes/bench/matrix/xl_clean.db \
        --labels benchmarks/results/located_n1000_xl_clean_rrf.jsonl \
        --device auto \
        --out benchmarks/results/ab_whitening_dense_xl_clean.json
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


def load_doc_vectors(db_path: str) -> tuple[list[str], np.ndarray]:
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
    # Defensive renormalize (ingest writes normalized vectors already).
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, mat / norms


def load_labels(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                r = json.loads(line)
                rows.append({"query": r["query"], "gold": r["planted_gene_id"]})
    return rows


def encode_queries(queries: list[str], device: str, batch: int = 32) -> np.ndarray:
    from helix_context.backends.bgem3_codec import BGEM3Codec

    codec = BGEM3Codec(dim=1024, device=device)
    out = []
    t0 = time.time()
    for i in range(0, len(queries), batch):
        chunk = queries[i : i + batch]
        out.extend(codec.encode_batch(chunk, task="query"))
        print(
            f"  encoded {min(i + batch, len(queries))}/{len(queries)} "
            f"({time.time() - t0:.0f}s)",
            file=sys.stderr,
        )
    q = np.asarray(out, dtype=np.float32)
    norms = np.linalg.norm(q, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return q / norms


def whitening_map(doc_mat: np.ndarray, eps: float = 1e-6):
    """(mu, W) from Ledoit-Wolf shrinkage covariance of the doc vectors."""
    from sklearn.covariance import LedoitWolf

    lw = LedoitWolf().fit(doc_mat)
    cov = lw.covariance_.astype(np.float64)
    evals, evecs = np.linalg.eigh(cov)
    evals = np.clip(evals, eps, None)
    w = (evecs * (1.0 / np.sqrt(evals))) @ evecs.T  # Σ^(-1/2), symmetric
    return lw.location_.astype(np.float32), w.astype(np.float32), float(lw.shrinkage_)


def apply_whitening(mat: np.ndarray, mu: np.ndarray, w: np.ndarray) -> np.ndarray:
    out = (mat - mu) @ w
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return out / norms


def rank_metrics(sims: np.ndarray, gold_idx: np.ndarray) -> dict:
    """sims: (nq, nd) similarity matrix; gold_idx: (nq,) column of the gold."""
    # rank of gold = 1 + number of docs with strictly higher similarity
    gold_sims = sims[np.arange(len(gold_idx)), gold_idx]
    ranks = 1 + (sims > gold_sims[:, None]).sum(axis=1)
    return {
        "retrieval_at_1": float((ranks == 1).mean()),
        "retrieval_at_5": float((ranks <= 5).mean()),
        "retrieval_at_10": float((ranks <= 10).mean()),
        "mrr": float((1.0 / ranks).mean()),
        "median_gold_rank": int(np.median(ranks)),
    }


def separation_stats(
    q: np.ndarray, d: np.ndarray, gold_idx: np.ndarray, rng: np.random.Generator,
    n_random: int = 200_000,
) -> dict:
    gold_sims = np.einsum("ij,ij->i", q, d[gold_idx])
    qi = rng.integers(0, len(q), n_random)
    di = rng.integers(0, len(d), n_random)
    rand_sims = np.einsum("ij,ij->i", q[qi], d[di])
    # Stage-4 margin-over-random formula: mu + 3*sigma of random pairs.
    thr = float(rand_sims.mean() + 3.0 * rand_sims.std())
    # Rank-based AUC gold vs random (P(gold > random)).
    all_s = np.concatenate([gold_sims, rand_sims])
    order = all_s.argsort(kind="mergesort")
    ranks = np.empty(len(all_s)); ranks[order] = np.arange(1, len(all_s) + 1)
    # tie handling is negligible at float32 resolution over 200k pairs
    r_pos = ranks[: len(gold_sims)].sum()
    n_pos, n_neg = len(gold_sims), len(rand_sims)
    auc = (r_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    return {
        "gold_mean": float(gold_sims.mean()),
        "gold_p50": float(np.percentile(gold_sims, 50)),
        "gold_p10": float(np.percentile(gold_sims, 10)),
        "random_mean": float(rand_sims.mean()),
        "random_p90": float(np.percentile(rand_sims, 90)),
        "random_std": float(rand_sims.std()),
        "margin_over_random_threshold_mu_plus_3sigma": thr,
        "gold_frac_clearing_threshold": float((gold_sims > thr).mean()),
        "gold_vs_random_auc": float(auc),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bed-db", default="genomes/bench/matrix/xl_clean.db")
    ap.add_argument("--labels", required=True, help="located_n1000 JSONL")
    ap.add_argument("--device", default="auto", help="auto|cuda|cpu")
    ap.add_argument("--max-queries", type=int, default=0, help="0 = all")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--out", default="benchmarks/results/ab_whitening_dense_xl_clean.json"
    )
    args = ap.parse_args()

    device = args.device
    if device == "auto":
        try:
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"

    print(f"loading doc vectors from {args.bed_db}...", file=sys.stderr)
    ids, docs = load_doc_vectors(args.bed_db)
    id_to_idx = {g: i for i, g in enumerate(ids)}
    print(f"  {len(ids)} docs x {docs.shape[1]}d", file=sys.stderr)

    labels = load_labels(args.labels)
    labels = [r for r in labels if r["gold"] in id_to_idx]
    if args.max_queries:
        labels = labels[: args.max_queries]
    if not labels:
        raise SystemExit("no labeled queries with gold in this bed")
    gold_idx = np.array([id_to_idx[r["gold"]] for r in labels])
    print(f"  {len(labels)} labeled queries", file=sys.stderr)

    print(f"encoding queries on {device}...", file=sys.stderr)
    q_raw = encode_queries([r["query"] for r in labels], device)

    rng = np.random.default_rng(args.seed)

    # ── arm A: raw cosine ────────────────────────────────────────────
    t0 = time.time()
    sims_a = q_raw @ docs.T
    arm_a = {
        **rank_metrics(sims_a, gold_idx),
        **separation_stats(q_raw, docs, gold_idx, rng),
    }
    print(f"arm A (cosine) done in {time.time() - t0:.0f}s", file=sys.stderr)

    # ── arm B: Ledoit-Wolf whitened cosine ───────────────────────────
    t0 = time.time()
    mu, w, shrinkage = whitening_map(docs)
    docs_w = apply_whitening(docs, mu, w)
    q_w = apply_whitening(q_raw, mu, w)
    sims_b = q_w @ docs_w.T
    arm_b = {
        **rank_metrics(sims_b, gold_idx),
        **separation_stats(q_w, docs_w, gold_idx, rng),
    }
    print(
        f"arm B (whitened, shrinkage={shrinkage:.4f}) done in "
        f"{time.time() - t0:.0f}s",
        file=sys.stderr,
    )

    payload = {
        "benchmark": "ab_whitening_dense",
        "phase": "1a (J-space council move #4)",
        "bed_db": args.bed_db,
        "labels": args.labels,
        "n_docs": len(ids),
        "n_queries": len(labels),
        "dim": int(docs.shape[1]),
        "device": device,
        "ledoit_wolf_shrinkage": shrinkage,
        "arms": {"cosine": arm_a, "whitened": arm_b},
        "deltas": {
            k: round(arm_b[k] - arm_a[k], 4)
            for k in ("retrieval_at_1", "retrieval_at_5", "retrieval_at_10", "mrr",
                      "gold_frac_clearing_threshold", "gold_vs_random_auc")
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["deltas"], indent=2))
    print(f"-> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
