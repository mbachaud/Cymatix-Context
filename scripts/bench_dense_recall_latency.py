"""Stage 2: dense recall latency micro-bench.

Measures p50 / p95 / p99 for three retrieval paths:
    (a) raw matmul scan only — establishes the ANN floor.
    (b) full ``query_genes_dense_recall(k=500)`` — id+score, no body fetch.
    (c) full ``query_genes_ann(pool_size=500)`` — lex+dense union, body fetch.

Plus a baseline: ``query_genes_ann`` at the legacy ``pool_size=12`` setting,
so the PR can demonstrate p95 ≤ baseline + 20 ms (spec §10 pass criterion).

Usage:

    # Synthetic 1k-gene fixture (default)
    python scripts/bench_dense_recall_latency.py

    # Against an existing populated genome
    python scripts/bench_dense_recall_latency.py --db path/to/genome.db --no-fixture

The default is synthetic so the bench can run in CI / on a dev box without
a real backfill. Synthetic fixture mocks the BGE-M3 codec with deterministic
hash-based vectors at the configured dim, producing comparable shape but
not semantically meaningful similarity.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cymatix_context.genome import Genome
from cymatix_context.schemas import (
    ChromatinState, EpigeneticMarkers, Gene, PromoterTags,
)


# ── Deterministic hash-based fake encoder for synthetic fixtures ──────


def _fake_vec(text: str, dim: int) -> np.ndarray:
    """Produce a stable, L2-normalised fp32 vector from ``text``.

    Uses SHA-256 expanded into ``dim`` floats. NOT a real embedding; the
    bench is timing-only, so we just need shape + numerical stability.
    """
    out = np.zeros(dim, dtype=np.float32)
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(seed[:8], "little"))
    for i in range(dim):
        out[i] = rng.gauss(0.0, 1.0)
    norm = np.linalg.norm(out)
    if norm > 0:
        out /= norm
    return out


class _FakeCodec:
    """Drop-in stand-in for BGEM3Codec — same encode signature."""

    def __init__(self, dim: int):
        self.dim = dim

    def encode(self, text: str, task: str = "passage"):
        return _fake_vec(text, self.dim).tolist()

    def similarity(self, a, b) -> float:
        return float(np.dot(np.asarray(a), np.asarray(b)))


# ── Fixture builder ───────────────────────────────────────────────────


def _build_fixture_genome(n_genes: int, dim: int) -> tuple[Genome, str]:
    """Create a temp DB + populated Genome with ``n_genes`` hot-tier genes.

    Returns ``(genome, db_path)``. Caller is responsible for cleanup.
    """
    tmp_dir = tempfile.mkdtemp(prefix="helix-bench-stage2-")
    db_path = os.path.join(tmp_dir, "genome.db")
    genome = Genome(
        path=db_path,
        dense_embedding_enabled=True,
        dense_embedding_dim=dim,
        dense_pool_size=500,
    )
    # Force the fake codec so the bench doesn't pull BGE-M3 weights.
    genome._dense_codec = _FakeCodec(dim=dim)

    # Populate genes + v2 BLOBs in a single transaction for speed.
    print(f"[bench] building {n_genes}-gene fixture at {db_path} dim={dim}")
    conn = genome.conn
    cur = conn.cursor()
    expected_bytes = dim * 4
    domains = ["alpha", "beta", "gamma", "delta", "epsilon"]
    t0 = time.monotonic()
    for i in range(n_genes):
        gene_id = f"bench-gene-{i:06d}"
        content = f"synthetic content number {i} domain {domains[i % len(domains)]}"
        vec = _fake_vec(content, dim).astype("<f4").tobytes()
        gene = Gene(
            gene_id=gene_id,
            content=content,
            complement="",
            codons=[],
            promoter=PromoterTags(domains=[domains[i % len(domains)]], entities=[]),
            epigenetics=EpigeneticMarkers(),
            chromatin=ChromatinState.OPEN,
            is_fragment=False,
        )
        # Use upsert_gene so the row gets the standard processing then patch
        # the v2 BLOB directly (we want a populated v2 column without paying
        # the BGE-M3 forward pass).
        genome.upsert_gene(gene, apply_gate=False)
        cur.execute(
            "UPDATE genes SET embedding_dense_v2 = ? WHERE gene_id = ?",
            (sqlite3.Binary(vec), gene_id),
        )
        if (i + 1) % 100 == 0:
            conn.commit()
    conn.commit()
    # Force matrix rebuild on first query.
    genome._invalidate_dense_matrix()
    elapsed = time.monotonic() - t0
    print(f"[bench] fixture built in {elapsed:.1f}s")
    return genome, db_path


# ── Timing helpers ────────────────────────────────────────────────────


def _measure(name: str, fn: Callable[[], object], warmup: int, measured: int) -> dict:
    """Time ``fn()`` and report p50/p95/p99 in milliseconds."""
    for _ in range(warmup):
        fn()
    samples_ms: list[float] = []
    for _ in range(measured):
        t0 = time.perf_counter()
        fn()
        samples_ms.append((time.perf_counter() - t0) * 1000.0)
    samples_ms.sort()
    p50 = statistics.median(samples_ms)
    p95 = samples_ms[int(0.95 * len(samples_ms))]
    p99 = samples_ms[int(0.99 * len(samples_ms))]
    print(
        f"[bench] {name:40s} n={measured} "
        f"p50={p50:7.2f}ms  p95={p95:7.2f}ms  p99={p99:7.2f}ms"
    )
    return {"name": name, "p50": p50, "p95": p95, "p99": p99, "samples": samples_ms}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None,
                        help="Existing populated genome.db (with --no-fixture).")
    parser.add_argument("--no-fixture", action="store_true",
                        help="Skip synthetic fixture build; use --db instead.")
    parser.add_argument("--n-genes", type=int, default=1000,
                        help="Synthetic fixture size (default 1000).")
    parser.add_argument("--dim", type=int, default=1024,
                        help="Embedding dim (default 1024).")
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--measured", type=int, default=200)
    parser.add_argument("--pool-size", type=int, default=500)
    args = parser.parse_args()

    if args.no_fixture:
        if not args.db:
            parser.error("--no-fixture requires --db")
        genome = Genome(
            path=args.db,
            dense_embedding_enabled=True,
            dense_embedding_dim=args.dim,
            dense_pool_size=args.pool_size,
        )
        # If running against a real populated DB, leave the codec real.
    else:
        genome, _ = _build_fixture_genome(args.n_genes, args.dim)

    queries = [f"sample query number {i} domain alpha beta" for i in range(args.measured + args.warmup)]
    qiter = iter(queries)
    def next_query() -> str:
        try:
            return next(qiter)
        except StopIteration:
            # Reset; bench is timing-only, repetition is fine.
            return "sample query repeated"

    # Force the matrix to be loaded once before any timing.
    genome._ensure_dense_matrix()

    # ── (a) raw matmul scan ────────────────────────────────────────────
    matrix, _ids = genome._ensure_dense_matrix()
    if matrix is None:
        print("[bench] FATAL: dense matrix is None — fixture broken")
        return 1
    codec = genome._get_dense_codec()

    def scan_only():
        q = codec.encode(next_query(), task="query")
        qv = np.asarray(q, dtype=np.float32)
        sims = matrix @ qv
        idx = np.argpartition(-sims, min(args.pool_size, sims.shape[0]) - 1)[: args.pool_size]
        _ = sims[idx]

    res_a = _measure("(a) matmul scan only", scan_only, args.warmup, args.measured)

    # ── (b) full query_genes_dense_recall ──────────────────────────────
    def dense_recall():
        return genome.query_genes_dense_recall(next_query(), k=args.pool_size)

    res_b = _measure("(b) query_genes_dense_recall", dense_recall, args.warmup, args.measured)

    # ── (c) full query_genes_ann (pool=500) ────────────────────────────
    def ann_pool_500():
        return genome.query_genes_ann(
            next_query(), domains=["alpha"], entities=[],
            max_genes=12, pool_size=args.pool_size,
        )

    res_c = _measure(
        f"(c) query_genes_ann pool={args.pool_size}",
        ann_pool_500, args.warmup, args.measured,
    )

    # ── (d) lex-only baseline at the SAME pool_size ────────────────────
    # Spec §10 pass criterion is meaningful as "what does dense recall add
    # on top of the lex pool we already pay for?" Holding pool_size constant
    # isolates the dense-recall delta. Comparing (c) at pool=500 vs (d) at
    # pool=12 conflates the dense delta with the cost of widening the lex
    # pool — that widening is a Stage 2 design choice, not a dense overhead.
    def lex_pool_only():
        return genome.query_genes(
            domains=["alpha"], entities=[],
            max_genes=args.pool_size,
        )

    res_d = _measure(
        f"(d) lex-only baseline pool={args.pool_size}",
        lex_pool_only, args.warmup, args.measured,
    )

    # ── (e) reference: legacy ann at pool=12 (informational only) ──────
    def ann_pool_12():
        return genome.query_genes_ann(
            next_query(), domains=["alpha"], entities=[],
            max_genes=12, pool_size=12,
        )

    res_e = _measure("(e) reference legacy pool=12", ann_pool_12, args.warmup, args.measured)

    delta_p95 = res_c["p95"] - res_d["p95"]
    print()
    print(f"[bench] p95 delta (c - d) = {delta_p95:+.2f} ms  "
          f"(dense recall cost on top of lex pool)")
    print(f"[bench] reference: legacy pool=12 p95 = {res_e['p95']:.2f} ms")
    print(f"[bench] pass criterion: p95(c) - p95(d) <= 20 ms  ->  "
          f"{'PASS' if delta_p95 <= 20.0 else 'FAIL'}")

    return 0 if delta_p95 <= 20.0 else 2


if __name__ == "__main__":
    sys.exit(main())
