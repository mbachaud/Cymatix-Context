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

Shared backfill loop
--------------------
The encode-and-pack loop lives in :func:`backfill_dense_db`. Tier-0 PR-2
(2026-05-16) made ``scripts/build_fixture_matrix.py`` import that function
and run it as a post-build pass on every freshly-built fixture ``.db`` so
the bench measures the real (dense-populated) pipeline rather than a
dense-dark one. The fixture builder and this operator script therefore
share a single backfill implementation and cannot drift.
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


def backfill_dense_db(
    db_path: str,
    *,
    dim: int | None = None,
    batch: int = 64,
    limit: int | None = None,
    codec=None,
    log_fn=print,
) -> dict:
    """Backfill ``genes.embedding_dense_v2`` on the SQLite DB at ``db_path``.

    This is the single shared implementation of the "open a ``.db``, find
    ``genes`` rows whose ``embedding_dense_v2`` is NULL or wrong-length,
    batch-encode their content, UPDATE the column" loop. Both this script's
    :func:`main` and ``scripts/build_fixture_matrix.py`` (Tier-0 PR-2) call
    it so the operator backfill and the fixture-builder post-build pass
    cannot drift.

    The loop reuses PR-1's canonical helpers from
    ``helix_context.backends.bgem3_codec``: :data:`PASSAGE_CHAR_CAP` (the
    passage input char cap), :meth:`BGEM3Codec.encode_batch` (the batch
    encoder) and :func:`vec_to_blob` (the fp32 packer). A genome backfilled
    here therefore satisfies the same ``length(blob) == dim*4`` idempotency
    skip-clause as one written by the inline-ingest path.

    Idempotent: rows that already carry a v2 BLOB of exactly ``dim*4`` bytes
    are not re-encoded, so a re-run reports ``rows_processed == 0``.

    Args:
        db_path: path to the genome ``.db`` to backfill in place.
        dim: dense vector dimension. ``None`` → ``retrieval.dense_embedding_dim``
            from ``helix.toml``.
        batch: encode + commit batch size. Default 64.
        limit: optional cap on rows to process (smoke tests).
        codec: an object exposing ``encode_batch(texts, task=...)``. ``None``
            → a fresh :class:`BGEM3Codec` at ``dim``. Tests inject a fake.
        log_fn: callable taking one string for progress lines. Default
            :func:`print`; pass a no-op (``lambda _msg: None``) to silence.

    Returns:
        A coverage report dict with keys: ``db_path``, ``dim``,
        ``expected_bytes``, ``total`` (genes rows), ``populated_before``,
        ``populated_after`` (rows with a correct-length v2 BLOB),
        ``rows_processed``, ``rows_skipped``, ``dense_coverage`` (float in
        ``[0.0, 1.0]`` = ``populated_after / total``) and ``elapsed_s``.
    """
    if dim is None:
        dim = int(load_config().retrieval.dense_embedding_dim)
    dim = int(dim)
    expected_bytes = dim * 4
    if codec is None:
        codec = BGEM3Codec(dim=dim)

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    _ensure_v2_schema(conn)
    cur = conn.cursor()

    total = cur.execute("SELECT COUNT(*) AS c FROM genes").fetchone()["c"]
    populated_before = cur.execute(
        "SELECT COUNT(*) AS c FROM genes WHERE embedding_dense_v2 IS NOT NULL"
    ).fetchone()["c"]
    log_fn(
        f"[backfill] {db_path}: genes total={total} "
        f"v2_populated_before={populated_before} dim={dim}"
    )

    # Idempotency: skip rows that already have a v2 BLOB of the right length.
    # ``length(blob) != expected_bytes`` guards half-written rows from an
    # earlier crash or a dim-change re-run, and agrees with the inline-ingest
    # write path's encoding so a PR-1-built genome selects 0 rows here.
    sql = (
        "SELECT gene_id, content FROM genes "
        "WHERE embedding_dense_v2 IS NULL "
        "   OR length(embedding_dense_v2) != ?"
    )
    params: tuple = (expected_bytes,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (expected_bytes, int(limit))
    rows = cur.execute(sql, params).fetchall()
    log_fn(f"[backfill] {db_path}: rows to process: {len(rows)}")

    t0 = time.monotonic()
    processed = 0
    skipped = 0
    for start in range(0, len(rows), max(1, batch)):
        chunk = rows[start:start + max(1, batch)]
        encodable = [
            (r["gene_id"], (r["content"] or ""))
            for r in chunk
            if (r["content"] or "").strip()
        ]
        skipped += len(chunk) - len(encodable)
        if not encodable:
            continue
        # Match the inline-ingest encode behaviour: bound each passage at
        # PASSAGE_CHAR_CAP and batch-encode with task="passage". The real
        # ``BGEM3Codec`` exposes ``encode_batch`` (PR-1) — the preferred,
        # one-model-call path; fall back to per-text ``encode`` for any
        # codec that predates it. ``encode_batch`` is byte-identical to a
        # sequence of ``encode`` calls (see bgem3_codec.encode_batch), so
        # the fallback does not change the stored vectors.
        texts = [content[:PASSAGE_CHAR_CAP] for _gid, content in encodable]
        if hasattr(codec, "encode_batch"):
            vecs = codec.encode_batch(texts, task="passage")
        else:
            vecs = [codec.encode(t, task="passage") for t in texts]
        updates = []
        for (gene_id, _content), vec in zip(encodable, vecs):
            try:
                blob = vec_to_blob(vec, dim)
            except ValueError as e:
                log_fn(f"[backfill] WARN: gene_id={gene_id} dim mismatch: {e}")
                skipped += 1
                continue
            updates.append((sqlite3.Binary(blob), gene_id))
        if updates:
            cur.executemany(
                "UPDATE genes SET embedding_dense_v2 = ? WHERE gene_id = ?",
                updates,
            )
            processed += len(updates)
        conn.commit()
        elapsed = time.monotonic() - t0
        rate = processed / elapsed if elapsed > 0 else 0.0
        log_fn(
            f"[backfill] {db_path}: {min(start + len(chunk), len(rows))}/{len(rows)} "
            f"processed={processed} skipped={skipped} rate={rate:.1f} genes/s"
        )

    conn.commit()
    elapsed = time.monotonic() - t0

    populated_after = cur.execute(
        "SELECT COUNT(*) AS c FROM genes WHERE embedding_dense_v2 IS NOT NULL "
        "AND length(embedding_dense_v2) = ?",
        (expected_bytes,),
    ).fetchone()["c"]
    conn.close()

    coverage = (populated_after / total) if total else 0.0
    log_fn(
        f"[backfill] {db_path}: DONE processed={processed} skipped={skipped} "
        f"v2_populated_after={populated_after} "
        f"coverage={100.0 * coverage:.2f}% elapsed={elapsed:.1f}s"
    )
    return {
        "db_path": db_path,
        "dim": dim,
        "expected_bytes": expected_bytes,
        "total": total,
        "populated_before": populated_before,
        "populated_after": populated_after,
        "rows_processed": processed,
        "rows_skipped": skipped,
        "dense_coverage": coverage,
        "elapsed_s": round(elapsed, 1),
    }


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

    print(f"[backfill] DB: {db_path}")
    print(f"[backfill] dim={dim} expected_bytes_per_row={dim * 4}")

    # Delegate to the shared backfill loop so this operator script and the
    # fixture builder (build_fixture_matrix.py, Tier-0 PR-2) cannot drift.
    backfill_dense_db(
        db_path,
        dim=dim,
        batch=args.batch,
        limit=args.limit,
        log_fn=print,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
