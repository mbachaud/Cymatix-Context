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
# Sibling-module import (crawl_watchdog) must resolve whether this file is
# run directly or imported as a module by build_fixture_matrix / the tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Issue #212: crawl watchdog -- throughput-triggered escalation ladder. The
# detector is pure; the CUDA helpers are guarded no-ops on CPU-only boxes.
from crawl_watchdog import (
    ACTION_DEMOTE,
    ACTION_EMPTY_CACHE,
    LOG_PREFIX as CRAWL_LOG_PREFIX,
    CrawlDetector,
    cuda_vram_fraction,
    release_cuda_cache,
)
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
    model_name: str | None = None,
    char_cap: int | None = None,
    batch: int = 64,
    limit: int | None = None,
    codec=None,
    log_fn=print,
    crawl_detector=None,
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
    passage input char cap, now the byte-identical default anchor for
    ``char_cap`` below — #207 dense fast-follow), :meth:`BGEM3Codec.encode_batch`
    (the batch encoder) and :func:`vec_to_blob` (the fp32 packer). A genome
    backfilled here therefore satisfies the same ``length(blob) == dim*4``
    idempotency skip-clause as one written by the inline-ingest path.

    Idempotent: rows that already carry a v2 BLOB of exactly ``dim*4`` bytes
    are not re-encoded, so a re-run reports ``rows_processed == 0``.

    Args:
        db_path: path to the genome ``.db`` to backfill in place.
        dim: dense vector dimension. ``None`` → ``retrieval.dense_embedding_dim``
            from ``helix.toml``.
        model_name: BGE-M3 model ID passed to ``BGEM3Codec``. ``None`` →
            ``retrieval.dense_model`` from ``helix.toml`` (#207 dense
            fast-follow; default ``"BAAI/bge-m3"``, byte-identical to the
            prior hardwired literal).
        char_cap: passage char cap applied before encoding — MUST stay
            identical to the inline-ingest slice
            (``context_manager.ingest``) and the query-side slice
            (``KnowledgeStore._encode_dense_v2_blob``) so the three encode
            paths cannot drift. ``None`` → ``ingestion.dense_passage_char_cap``
            from ``helix.toml``, falling back to the module constant
            :data:`PASSAGE_CHAR_CAP` if a stale config object lacks the key.
        batch: encode + commit batch size. Default 64.
        limit: optional cap on rows to process (smoke tests).
        codec: an object exposing ``encode_batch(texts, task=...)``. ``None``
            → a fresh :class:`BGEM3Codec` at ``dim``. Tests inject a fake.
        log_fn: callable taking one string for progress lines. Default
            :func:`print`; pass a no-op (``lambda _msg: None``) to silence.
        crawl_detector: an object exposing ``feed(genes, dt, vram_frac)``
            (the issue #212 crawl watchdog). ``None`` -> a
            :class:`crawl_watchdog.CrawlDetector` built from the
            ``HELIX_BFM_CRAWL_*`` env knobs -- honored HERE so both the
            standalone operator script and the fixture builder's
            ``_backfill_dense`` pass get the watchdog. On a sustained
            crawl (per-batch genes/s EMA below the run's own early-batch
            baseline / ``HELIX_BFM_CRAWL_FACTOR`` for
            ``HELIX_BFM_CRAWL_WINDOW`` consecutive batches with dedicated
            VRAM > 0.92 of capacity) the ladder first releases the CUDA
            cache, then tears the codec down and reloads it on CPU for
            the REMAINDER of this DB (``BGEM3_DEVICE=cpu`` semantics,
            byte-identical vectors). Tests inject a fake.

    Returns:
        A coverage report dict with keys: ``db_path``, ``dim``,
        ``expected_bytes``, ``total`` (genes rows), ``populated_before``,
        ``populated_after`` (rows with a correct-length v2 BLOB),
        ``rows_processed``, ``rows_skipped``, ``dense_coverage`` (float in
        ``[0.0, 1.0]`` = ``populated_after / total``) and ``elapsed_s``.
    """
    if dim is None or model_name is None or char_cap is None:
        # #207 dense fast-follow: resolve all three from the SAME loaded
        # config so an operator run and a caller-supplied override cannot
        # partially mix stale/fresh values.
        _cfg = load_config()
        if dim is None:
            dim = int(_cfg.retrieval.dense_embedding_dim)
        if model_name is None:
            model_name = str(_cfg.retrieval.dense_model)
        if char_cap is None:
            char_cap = int(getattr(
                _cfg.ingestion, "dense_passage_char_cap", PASSAGE_CHAR_CAP
            ))
    dim = int(dim)
    char_cap = int(char_cap)
    expected_bytes = dim * 4
    # Issue #212: only a codec WE constructed can be torn down and reloaded
    # on CPU by the crawl watchdog's terminal rung. A caller-injected codec
    # (tests, exotic embedders) is left alone -- demote becomes log-only.
    owns_codec = codec is None
    if codec is None:
        # Backfill codec device, in priority order:
        #   1. an explicit ``BGEM3_DEVICE`` env var — operator override (#134);
        #   2. auto-detected CUDA when a GPU is visible (Tier-0 455961c);
        #   3. CPU otherwise.
        # ``BGEM3Codec`` defaults to ``device="cpu"``; on CPU this
        # encode-and-pack loop is a half-day job at ~19k genes, vs ~5-15 min
        # on a GPU (the speeds this script's header advertises). The codec's
        # sentence-transformers backend forwards ``device`` to the model.
        # Auto-detect as the default — rather than a hard ``"cuda"`` — lets a
        # CPU-only host with no env var degrade gracefully instead of failing
        # to place the model. ``BGEM3_DEVICE`` empty or ``"auto"`` forces the
        # auto-detect path. Tests inject their own ``codec`` and bypass this.
        import os
        device = os.environ.get("BGEM3_DEVICE", "").strip()
        if not device or device.lower() == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:  # noqa: BLE001 — torch missing → CPU is the only option
                device = "cpu"
        codec = BGEM3Codec(dim=dim, device=device, model_name=model_name)
        log_fn(f"[backfill] codec device={device} model={model_name}")

    # Single connection for the whole backfill. Wrapped in try/finally so a
    # raise anywhere below (most likely ``codec.encode_batch`` when the model
    # is unavailable) still closes it — a leaked WAL connection holds
    # ``-wal``/``-shm`` locks on Windows.
    conn = sqlite3.connect(db_path, timeout=30)
    try:
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
        # Issue #212 crawl watchdog: one observation per batch, env knobs
        # (HELIX_BFM_CRAWL_WINDOW / _FACTOR / _ACTION) honored here so the
        # standalone script and build_fixture_matrix._backfill_dense share
        # one implementation. CPU-only boxes never trip (vram probe = None).
        watchdog = (
            crawl_detector
            if crawl_detector is not None
            else CrawlDetector.from_env(log_fn=log_fn, name=Path(db_path).name)
        )
        demoted = False
        last_feed_t = t0
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
            # char_cap (#207 dense fast-follow — config-resolved, default
            # byte-identical to PASSAGE_CHAR_CAP) and batch-encode with
            # task="passage". The real ``BGEM3Codec`` exposes ``encode_batch``
            # (PR-1) — the preferred, one-model-call path; fall back to
            # per-text ``encode`` for any codec that predates it.
            # ``encode_batch`` is byte-identical to a sequence of ``encode``
            # calls (see bgem3_codec.encode_batch), so the fallback does not
            # change the stored vectors.
            texts = [content[:char_cap] for _gid, content in encodable]
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
            # Issue #212: feed the crawl watchdog and apply its escalation
            # ladder at this batch boundary.
            now = time.monotonic()
            crawl_action = watchdog.feed(
                len(encodable), now - last_feed_t, cuda_vram_fraction(),
            )
            last_feed_t = now
            if crawl_action == ACTION_EMPTY_CACHE:
                released = release_cuda_cache()
                log_fn(
                    f"{CRAWL_LOG_PREFIX} rung 1 applied: gc.collect + "
                    f"torch.cuda.empty_cache (released={released})"
                )
            elif crawl_action == ACTION_DEMOTE and not demoted:
                demoted = True
                if owns_codec:
                    log_fn(
                        f"{CRAWL_LOG_PREFIX} DEMOTING to CPU: tearing down the "
                        f"codec and reloading with device=cpu for the remainder "
                        f"of {db_path} (BGEM3_DEVICE=cpu semantics -- vectors "
                        f"are byte-identical, no VRAM ceiling; see "
                        f"docs/operations/DENSE_VRAM.md)"
                    )
                    codec = None
                    release_cuda_cache()
                    codec = BGEM3Codec(dim=dim, device="cpu", model_name=model_name)
                    log_fn(
                        f"{CRAWL_LOG_PREFIX} codec reloaded on CPU; resuming "
                        f"backfill of {db_path}"
                    )
                else:
                    log_fn(
                        f"{CRAWL_LOG_PREFIX} demote requested but the codec was "
                        f"injected by the caller -- cannot rebuild it on CPU; "
                        f"continuing as-is"
                    )

        conn.commit()
        elapsed = time.monotonic() - t0

        populated_after = cur.execute(
            "SELECT COUNT(*) AS c FROM genes WHERE embedding_dense_v2 IS NOT NULL "
            "AND length(embedding_dense_v2) = ?",
            (expected_bytes,),
        ).fetchone()["c"]
    finally:
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
    # #207 dense fast-follow: model ID + passage cap, resolved from the same
    # loaded config as dim above.
    model_name = str(cfg.retrieval.dense_model)
    char_cap = int(getattr(cfg.ingestion, "dense_passage_char_cap", PASSAGE_CHAR_CAP))

    print(f"[backfill] DB: {db_path}")
    print(
        f"[backfill] dim={dim} expected_bytes_per_row={dim * 4} "
        f"model={model_name} char_cap={char_cap}"
    )

    # Delegate to the shared backfill loop so this operator script and the
    # fixture builder (build_fixture_matrix.py, Tier-0 PR-2) cannot drift.
    backfill_dense_db(
        db_path,
        dim=dim,
        model_name=model_name,
        char_cap=char_cap,
        batch=args.batch,
        limit=args.limit,
        log_fn=print,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
