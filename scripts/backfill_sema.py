"""Backfill ``genes.embedding`` — the 20D SEMA vector, packed fp32 BLOB.

Writes ``genes.embedding`` for every row whose embedding is currently NULL
and whose content is non-NULL. Idempotent: a row that already carries a
BLOB is not selected on a re-run (the query filters on ``embedding IS
NULL``), so re-running this script after a partial run only touches the
rows still missing a vector.

Usage:

    python scripts/backfill_sema.py --db genomes/main/genome.db
    python scripts/backfill_sema.py --db genome.db --batch 128 --limit 5000
    python scripts/backfill_sema.py --db genome.db --dry-run

Codec resolution mirrors ``CymatixContextManager.__init__`` exactly: when
an encoder daemon URL is active (``encoder_client.active_url()`` truthy —
env ``CYMATIX_ENCODER_URL`` over the ``[encoder_daemon]`` config slot) the
script encodes through ``encoder_client.RemoteSemaCodec`` (HTTP, with its
own explicit per-request timeouts and in-process fallback on outage);
otherwise it builds a local ``LazySemaCodec`` (sentence-transformers
MiniLM, loaded lazily on first encode). Both classes are reused verbatim
from ``cymatix_context.backends`` — this script does not reimplement
encoding.

The encode input is capped at ``content[:1000]`` — the same cap
``CymatixContextManager`` applies at ingest time (``context_manager.py``,
the ``sema_codec.encode_batch`` call sites) — so a gene backfilled here
gets the byte-identical vector it would have received had SEMA been
available at ingest.

Shared backfill loop
---------------------
The encode-and-pack loop lives in :func:`backfill_sema_db` so tests can
inject a fake codec (mirrors ``scripts/backfill_bgem3_v2.py``'s
``codec=`` parameter) without needing sentence-transformers installed.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cymatix_context.backends.sema_codec import sema_vec_to_blob

log = logging.getLogger("cymatix.backfill_sema")

# Matches the inline literal at the ingest write path
# (``context_manager.py``, ``self._sema_codec.encode(gene.content[:1000])``
# / ``encode_batch``) — a backfilled vector must be encoded from the same
# slice as the live ingest path or the two would silently diverge.
SEMA_CHAR_CAP = 1000


def _resolve_codec(log_fn) -> Any:
    """Build a SEMA codec the same way ``CymatixContextManager`` does.

    ``RemoteSemaCodec`` when an encoder daemon URL is active, else
    ``LazySemaCodec`` — both classes reused verbatim, never reimplemented.
    """
    from cymatix_context.backends import encoder_client
    from cymatix_context.backends.sema import LazySemaCodec
    from cymatix_context.config import load_config

    cfg = load_config()
    model_name = str(getattr(cfg.ingestion, "sema_model", "all-MiniLM-L6-v2"))
    encoder_url = encoder_client.active_url()
    if encoder_url:
        log_fn(f"[backfill] codec=RemoteSemaCodec url={encoder_url} model={model_name}")
        return encoder_client.RemoteSemaCodec(model_name=model_name, url=encoder_url)
    log_fn(f"[backfill] codec=LazySemaCodec model={model_name}")
    return LazySemaCodec(model_name=model_name)


def backfill_sema_db(
    db_path: str,
    *,
    batch: int = 256,
    limit: int = 0,
    dry_run: bool = False,
    codec: Optional[Any] = None,
    log_fn=print,
) -> dict:
    """Backfill ``genes.embedding`` (SEMA 20D BLOB) on the SQLite DB at ``db_path``.

    Args:
        db_path: path to the genome ``.db`` to backfill in place.
        batch: encode + commit batch size. Default 256.
        limit: cap on rows selected; ``0`` (default) means all matching rows.
        dry_run: count matching rows and report, without encoding or writing.
        codec: an object exposing ``encode_batch(texts)`` -> list[list[float]].
            ``None`` (default) resolves the same codec the manager would
            build (see :func:`_resolve_codec`). Tests inject a fake.
        log_fn: callable taking one string for progress lines. Default
            :func:`print`; pass a no-op to silence.

    Returns:
        A summary dict with keys ``db_path``, ``total_candidates``,
        ``backfilled``, ``skipped``, ``remaining`` (genes rows still
        ``embedding IS NULL`` after the run), ``elapsed_s``, ``dry_run``.

    Batch failures are soft: an encode-batch that raises, or returns a
    malformed/wrong-shape payload, is logged via ``log.warning`` and the
    whole batch is counted as skipped — the loop continues to the next
    batch rather than aborting the run.
    """
    # WAL-friendly: one writer connection for the whole run, busy_timeout
    # set so a concurrent reader/writer backs off instead of raising
    # "database is locked" immediately.
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        cur = conn.cursor()

        sql = "SELECT gene_id, content FROM genes WHERE embedding IS NULL AND content IS NOT NULL"
        params: tuple = ()
        if limit and limit > 0:
            sql += " LIMIT ?"
            params = (int(limit),)
        rows = cur.execute(sql, params).fetchall()
        total_candidates = len(rows)
        log_fn(f"[backfill] {db_path}: candidates (embedding NULL, content NOT NULL) = {total_candidates}")

        if dry_run:
            remaining = cur.execute(
                "SELECT COUNT(*) FROM genes WHERE embedding IS NULL"
            ).fetchone()[0]
            log_fn(
                f"[backfill] {db_path}: DRY RUN — would encode up to "
                f"{total_candidates} rows; no writes made"
            )
            return {
                "db_path": db_path,
                "total_candidates": total_candidates,
                "backfilled": 0,
                "skipped": 0,
                "remaining": remaining,
                "elapsed_s": 0.0,
                "dry_run": True,
            }

        if codec is None:
            codec = _resolve_codec(log_fn)

        t0 = time.monotonic()
        backfilled = 0
        skipped = 0
        for start in range(0, len(rows), max(1, batch)):
            chunk = rows[start:start + max(1, batch)]
            encodable = [
                (r["gene_id"], (r["content"] or "")[:SEMA_CHAR_CAP])
                for r in chunk
                if (r["content"] or "").strip()
            ]
            blank = len(chunk) - len(encodable)
            skipped += blank

            if encodable:
                texts = [content for _gid, content in encodable]
                try:
                    vecs = codec.encode_batch(texts)
                except Exception as exc:  # noqa: BLE001 — soft-fail per batch
                    log.warning(
                        "batch at rows[%d:%d] failed to encode (%s); skipping %d rows",
                        start, start + len(chunk), exc, len(encodable),
                    )
                    skipped += len(encodable)
                    vecs = None

                if vecs is not None:
                    if not isinstance(vecs, list) or len(vecs) != len(encodable):
                        log.warning(
                            "batch at rows[%d:%d] returned a malformed payload "
                            "(expected %d vectors); skipping",
                            start, start + len(chunk), len(encodable),
                        )
                        skipped += len(encodable)
                    else:
                        updates = []
                        for (gene_id, _content), vec in zip(encodable, vecs):
                            try:
                                blob = sqlite3.Binary(sema_vec_to_blob(vec))
                            except Exception as exc:  # noqa: BLE001 — bad vector shape
                                log.warning(
                                    "gene_id=%s: failed to pack SEMA vector (%s); skipping",
                                    gene_id, exc,
                                )
                                skipped += 1
                                continue
                            updates.append((blob, gene_id))
                        if updates:
                            cur.executemany(
                                "UPDATE genes SET embedding = ? WHERE gene_id = ?",
                                updates,
                            )
                            backfilled += len(updates)

            # Commit per batch, whether or not this batch produced writes —
            # keeps the WAL small and makes the run resumable if killed
            # mid-way (already-committed rows are no longer NULL, so a
            # re-run's WHERE clause naturally skips them).
            conn.commit()

            elapsed = time.monotonic() - t0
            rate = backfilled / elapsed if elapsed > 0 else 0.0
            log_fn(
                f"[backfill] {db_path}: {min(start + len(chunk), len(rows))}/{len(rows)} "
                f"backfilled={backfilled} skipped={skipped} rate={rate:.1f} enc/s"
            )

        elapsed = time.monotonic() - t0
        remaining = cur.execute(
            "SELECT COUNT(*) FROM genes WHERE embedding IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    log_fn(
        f"[backfill] {db_path}: DONE backfilled={backfilled} skipped={skipped} "
        f"remaining={remaining} elapsed={elapsed:.1f}s"
    )
    return {
        "db_path": db_path,
        "total_candidates": total_candidates,
        "backfilled": backfilled,
        "skipped": skipped,
        "remaining": remaining,
        "elapsed_s": round(elapsed, 1),
        "dry_run": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill genes.embedding (SEMA 20D fp32 BLOB)."
    )
    parser.add_argument(
        "--db", required=True, dest="db_path",
        help="Path to genome.db to backfill in place.",
    )
    parser.add_argument(
        "--batch", type=int, default=256,
        help="Encode batch size (commit cadence). Default 256.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Cap on rows to process. 0 (default) = all matching rows.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count matching rows and report; make no writes.",
    )
    args = parser.parse_args()

    report = backfill_sema_db(
        args.db_path,
        batch=args.batch,
        limit=args.limit,
        dry_run=args.dry_run,
        log_fn=print,
    )
    print(
        f"[backfill] summary: backfilled={report['backfilled']} "
        f"skipped={report['skipped']} remaining={report['remaining']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
