"""Stage 2: backfill BGE-M3 v2 dense vectors as raw fp32 BLOBs.

Stage 2 of the helix-context retrieval-fix plan (2026-05-08).

Writes ``embedding_dense_v2`` (BLOB, raw little-endian fp32, ``dim*4`` bytes)
for every gene whose v2 column is currently NULL. Idempotent: rows with a
non-NULL v2 BLOB of the expected length are skipped.

Usage:

    python scripts/backfill_bgem3_v2.py [path/to/genome.db]

If no path is given, reads ``[genome] path`` from ``helix.toml``.

Operator runbook (post-merge):

    1. Make a snapshot copy of ``genomes/main/genome.db`` first.
    2. Run this script against the copy. Verify it reports ``coverage=100%``.
    3. Hot-swap the populated DB into place during a maintenance window.
    4. Stage 4 follows: recalibrate ``ann_similarity_threshold`` at dim=1024.

Wall-clock estimate at 18.9k genes on CPU sentence-transformers BGE-M3:
~30-90 minutes. With FlagEmbedding + GPU, ~5-15 minutes. Resumable via the
idempotent skip-clause.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helix_context.backends.bgem3_codec import (
    PASSAGE_CHAR_CAP,
    BGEM3Codec,
    vec_to_blob,
)
from helix_context.config import load_config


def _ensure_v2_schema(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER + partial index. Matches Genome._init_db()."""
    cur = conn.cursor()
    existing = {row[1] for row in cur.execute("PRAGMA table_info(genes)").fetchall()}
    if "embedding_dense_v2" not in existing:
        cur.execute("ALTER TABLE genes ADD COLUMN embedding_dense_v2 BLOB")
        print("[backfill] Added embedding_dense_v2 BLOB column")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_genes_dense_v2_hot "
        "ON genes(gene_id) "
        "WHERE embedding_dense_v2 IS NOT NULL AND chromatin < 2"
    )
    conn.commit()


# ``_vec_to_blob`` is kept as a thin alias of the canonical
# ``helix_context.backends.bgem3_codec.vec_to_blob`` so the inline-ingest
# write path (knowledge_store.upsert_doc) and this offline backfill share one
# encoding and cannot drift. See PR-1 of the 2026-05-16 Tier-0 plan.
_vec_to_blob = vec_to_blob


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill BGE-M3 v2 BLOBs.")
    parser.add_argument(
        "db_path", nargs="?", default=None,
        help="Path to genome.db (defaults to helix.toml [genome] path).",
    )
    parser.add_argument(
        "--batch", type=int, default=64,
        help="Encode batch size (commit cadence). Default 64.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Optional cap on rows to process (for smoke tests).",
    )
    parser.add_argument(
        "--dim", type=int, default=None,
        help="Override dim. Defaults to retrieval.dense_embedding_dim from config.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    cfg = load_config()
    db_path = args.db_path or str(repo_root / cfg.genome.path)
    dim = int(args.dim if args.dim is not None else cfg.retrieval.dense_embedding_dim)
    expected_bytes = dim * 4

    print(f"[backfill] DB: {db_path}")
    print(f"[backfill] dim={dim} expected_bytes_per_row={expected_bytes}")

    codec = BGEM3Codec(dim=dim)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    _ensure_v2_schema(conn)

    cur = conn.cursor()

    # Pre-flight coverage report.
    total = cur.execute("SELECT COUNT(*) AS c FROM genes").fetchone()["c"]
    populated_before = cur.execute(
        "SELECT COUNT(*) AS c FROM genes WHERE embedding_dense_v2 IS NOT NULL"
    ).fetchone()["c"]
    print(f"[backfill] genes total={total} v2_populated_before={populated_before}")

    # Idempotency: skip rows that already have a v2 BLOB of the right length.
    # ``length(blob) == expected_bytes`` guards against half-written rows from
    # an earlier crash or a dim-change re-run.
    sql = (
        "SELECT gene_id, content FROM genes "
        "WHERE embedding_dense_v2 IS NULL "
        "   OR length(embedding_dense_v2) != ?"
    )
    params: tuple = (expected_bytes,)
    if args.limit is not None:
        sql += " LIMIT ?"
        params = (expected_bytes, int(args.limit))
    rows = cur.execute(sql, params).fetchall()
    print(f"[backfill] rows to process: {len(rows)}")

    t0 = time.monotonic()
    processed = 0
    skipped = 0
    for i, row in enumerate(rows):
        content = row["content"] or ""
        if not content.strip():
            skipped += 1
            continue
        # Match production encode behaviour: bound passage length at
        # PASSAGE_CHAR_CAP (BGE-M3 max_length=512 tokens, ~2k chars is a safe
        # cap). The inline-ingest path uses the same cap.
        vec = codec.encode(content[:PASSAGE_CHAR_CAP], task="passage")
        try:
            blob = _vec_to_blob(vec, dim)
        except ValueError as e:
            print(f"[backfill] WARN: gene_id={row['gene_id']} dim mismatch: {e}")
            skipped += 1
            continue
        cur.execute(
            "UPDATE genes SET embedding_dense_v2 = ? WHERE gene_id = ?",
            (sqlite3.Binary(blob), row["gene_id"]),
        )
        processed += 1
        if (i + 1) % args.batch == 0:
            conn.commit()
            elapsed = time.monotonic() - t0
            rate = processed / elapsed if elapsed > 0 else 0.0
            print(
                f"[backfill] {i+1}/{len(rows)} "
                f"processed={processed} skipped={skipped} "
                f"rate={rate:.1f} genes/s"
            )

    conn.commit()
    elapsed = time.monotonic() - t0

    # Post-flight coverage report.
    populated_after = cur.execute(
        "SELECT COUNT(*) AS c FROM genes WHERE embedding_dense_v2 IS NOT NULL "
        "AND length(embedding_dense_v2) = ?",
        (expected_bytes,),
    ).fetchone()["c"]
    coverage_pct = 100.0 * populated_after / total if total else 0.0
    print(
        f"[backfill] DONE. processed={processed} skipped={skipped} "
        f"v2_populated_after={populated_after} coverage={coverage_pct:.2f}% "
        f"elapsed={elapsed:.1f}s"
    )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
