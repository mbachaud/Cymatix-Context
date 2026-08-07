"""
Migrate genes_fts from the plain contentful FTS5 form to external-content.

Issue #338: the contentful ``genes_fts`` stores its own full copy of every
document in the ``genes_fts_content`` shadow — 8.67 GB / 18.5% of the
829K-gene blob bed, pure duplication of ``genes.content``. This script
rebuilds the index in the EXTERNAL-CONTENT form backed by the
``genes_fts_source`` view, which reproduces the exact composite text the
contentful table indexed (source_id + promoter tags + content,
complement). Tokenizer, BM25 statistics, and column reads are identical
— the rebuild is signal-neutral; only the redundant shadow copy is gone.

Offline and idempotent:
  - refuses to run on an already-migrated store (clean message, no-op)
  - a crash mid-migration rolls the whole transaction back to the
    untouched legacy table
  - ``--dry-run`` reports the expected reclaim without touching anything

STUB RETENTION WARNING (cc-exchange spark-erb-receipts 0017-joe §6,
2026-08-07): the contentful shadow accidentally retains every gene's
PRE-COMPRESSION original text — upsert indexes full content before
``compress_to_euchromatin`` stubs the ``genes`` row, and compression
never touches the FTS table. On a stubbed store this migration rebuilds
the index from the CURRENT (stub) text and permanently destroys the only
copy of those originals — and forecloses the proposed "serve stub
originals from the FTS shadow" read-back path (#327 comment thread).
The script therefore refuses to run when the store contains stubs unless
``--allow-stub-loss`` is passed. On stub-free beds (CPU tagger, the ERB
beds) the rebuild is byte-identical and this guard never fires.

The freed pages go to the SQLite freelist. Run VACUUM afterwards (or pass
``--vacuum``) to shrink the file — on big beds VACUUM rewrites the whole
database and needs up to 2x the file size in free disk.

Usage:
    # Report the expected reclaim, change nothing:
    python scripts/migrate_fts5_external_content.py --db genomes/main/genome.db --dry-run

    # Migrate (stop any server writing to this store first):
    python scripts/migrate_fts5_external_content.py --db genomes/main/genome.db

    # Migrate and reclaim the file space in one go:
    python scripts/migrate_fts5_external_content.py --db genomes/main/genome.db --vacuum
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

log = logging.getLogger(__name__)

# External-content over a VIEW needs SQLite >= 3.34.0 (2020-12).
MIN_SQLITE = (3, 34, 0)


def _ensure_root_on_path():
    """Make cymatix_context importable when run as a script."""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_ensure_root_on_path()

from cymatix_context.storage.ddl import (  # noqa: E402
    FTS_EXTERNAL_CREATE_SQL,
    FTS_SOURCE_VIEW,
    FTS_SOURCE_VIEW_SQL,
    fts5_is_external_content,
)


def _shadow_bytes(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Size of the ``genes_fts_content`` shadow — the reclaim target.

    Prefers ``dbstat`` (true page occupancy). The stock CPython sqlite on
    Windows lacks SQLITE_ENABLE_DBSTAT_VTAB (verified 2026-08-05, see
    benchmarks/dogfood/erb/storage_audit.py), so this falls back to
    summed byte lengths x 1.3 b-tree overhead — same estimator the
    storage audit uses.
    """
    dbstat_error = None
    try:
        row = conn.execute(
            "SELECT SUM(pgsize) FROM dbstat WHERE name = 'genes_fts_content'"
        ).fetchone()
        if row and row[0]:
            return {"bytes": int(row[0]), "measured": "exact(dbstat)"}
    except sqlite3.Error as exc:
        # Recorded in the report (mirrors storage_audit's exact_error) —
        # the estimator below is the expected path on stock CPython.
        dbstat_error = f"{type(exc).__name__}: {exc}"
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM("
            "  COALESCE(LENGTH(CAST(c0 AS BLOB)), 0) + "
            "  COALESCE(LENGTH(CAST(c1 AS BLOB)), 0) + "
            "  COALESCE(LENGTH(CAST(c2 AS BLOB)), 0)), 0) "
            "FROM genes_fts_content"
        ).fetchone()
    except sqlite3.Error as exc:
        return {
            "bytes": 0,
            "measured": f"unavailable ({exc})",
            "dbstat_error": dbstat_error,
        }
    rows, payload = int(row[0]), int(row[1])
    return {
        "bytes": int(payload * 1.3),
        "rows": rows,
        "measured": "estimated (payload bytes x 1.3 b-tree overhead)",
        "dbstat_error": dbstat_error,
    }


def _stub_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Count compression stubs — genes whose original text now exists ONLY
    in the contentful FTS shadow this migration is about to drop.

    ``compression_tier = 1`` was verified an exact synonym for the stub
    set on the analyzed LLM-tagger bed (0017-joe §6); the
    ``[COMPRESSED:%`` marker is the literal stub content
    ``compress_to_euchromatin`` writes. Counted with OR so either signal
    alone trips the guard — for a destructive step, over-refusing beats
    under-refusing. Older schemas without ``compression_tier`` fall back
    to the marker alone.
    """
    marker = int(conn.execute(
        "SELECT COUNT(*) FROM genes WHERE content LIKE '[COMPRESSED:%'"
    ).fetchone()[0])
    try:
        combined = int(conn.execute(
            "SELECT COUNT(*) FROM genes "
            "WHERE compression_tier = 1 OR content LIKE '[COMPRESSED:%'"
        ).fetchone()[0])
        tier = int(conn.execute(
            "SELECT COUNT(*) FROM genes WHERE compression_tier = 1"
        ).fetchone()[0])
    except sqlite3.Error as exc:
        log.warning(
            "compression_tier unreadable (%s) — stub guard counting the "
            "[COMPRESSED:%% marker only", exc,
        )
        combined, tier = marker, None
    return {
        "stub_rows": combined,
        "stub_rows_marker": marker,
        "stub_rows_tier1": tier,
    }


def _file_stats(conn: sqlite3.Connection) -> Dict[str, int]:
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    return {
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist,
        "file_bytes": page_size * page_count,
        "freelist_bytes": page_size * freelist,
    }


def _integrity_check(conn: sqlite3.Connection) -> None:
    """FTS5 integrity check; rank=1 also verifies index vs content view."""
    try:
        conn.execute(
            "INSERT INTO genes_fts(genes_fts, rank) VALUES ('integrity-check', 1)"
        )
    except sqlite3.OperationalError as exc:
        if "no such" in str(exc).lower():
            raise
        # Older SQLite without the two-arg form — internal check only.
        log.warning("two-arg integrity-check unsupported (%s); "
                    "running the internal-consistency form", exc)
        conn.execute("INSERT INTO genes_fts(genes_fts) VALUES ('integrity-check')")


def migrate(
    db_path: Union[str, Path],
    dry_run: bool = False,
    vacuum: bool = False,
    allow_stub_loss: bool = False,
) -> Dict[str, Any]:
    """Rebuild *db_path*'s genes_fts in external-content form.

    Returns a report dict with ``status`` one of ``migrated`` /
    ``already_migrated`` / ``dry_run`` / ``no_fts_table`` /
    ``refused_stub_loss`` (store has compression stubs and
    *allow_stub_loss* was not passed — see the module docstring).
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"knowledge store not found: {db_path}")

    ver = tuple(int(p) for p in sqlite3.sqlite_version.split("."))
    if ver < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old for view-backed "
            f"external-content FTS5 (need >= {'.'.join(map(str, MIN_SQLITE))})"
        )

    report: Dict[str, Any] = {
        "tool": "scripts/migrate_fts5_external_content.py",
        "db": str(db_path),
        "sqlite_version": sqlite3.sqlite_version,
        "dry_run": dry_run,
    }

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        exists = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'genes_fts'"
        ).fetchone() is not None
        if not exists:
            report["status"] = "no_fts_table"
            report["message"] = (
                "no genes_fts table in this store — open it with the current "
                "code once and the external-content form is created directly"
            )
            return report

        if fts5_is_external_content(cur):
            report["status"] = "already_migrated"
            report["message"] = (
                "genes_fts is already the external-content form — nothing to do"
            )
            return report

        gene_count = int(cur.execute("SELECT COUNT(*) FROM genes").fetchone()[0])
        fts_count = int(cur.execute("SELECT COUNT(*) FROM genes_fts").fetchone()[0])
        shadow = _shadow_bytes(conn)
        stubs = _stub_stats(conn)
        report["gene_rows"] = gene_count
        report["fts_rows_before"] = fts_count
        report["contentful_shadow"] = shadow
        report["estimated_reclaim_bytes"] = shadow["bytes"]
        report["stub_guard"] = stubs
        report["file_before"] = _file_stats(conn)

        stub_warning = None
        if stubs["stub_rows"] > 0:
            stub_warning = (
                f"{stubs['stub_rows']} of {gene_count} genes are compression "
                f"stubs (marker: {stubs['stub_rows_marker']}, "
                f"compression_tier=1: {stubs['stub_rows_tier1']}). Their "
                "PRE-COMPRESSION original text exists ONLY in the contentful "
                "FTS shadow and will be PERMANENTLY LOST by this rebuild "
                "(the external index re-indexes the current stub text). "
                "This also forecloses the proposed serve-stub-originals-"
                "from-FTS read-back (#327) on this store. Restore originals "
                "first, or pass --allow-stub-loss to proceed anyway."
            )
            report["stub_warning"] = stub_warning

        if dry_run:
            report["status"] = "dry_run"
            report["message"] = (
                f"would rebuild genes_fts ({fts_count} rows) in external-content "
                f"form and free ~{shadow['bytes'] / 1024 ** 3:.2f} GB "
                f"({shadow['measured']}); nothing was modified"
                + (f". STUB WARNING: {stub_warning}" if stub_warning else "")
            )
            return report

        if stub_warning and not allow_stub_loss:
            report["status"] = "refused_stub_loss"
            report["message"] = f"refusing to migrate: {stub_warning}"
            log.warning("%s", report["message"])
            return report
        if stub_warning:
            report["stub_loss_accepted"] = True
            log.warning("--allow-stub-loss: %s", stub_warning)

        had_vocab = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'genes_fts_vocab'"
        ).fetchone() is not None

        t0 = time.time()
        try:
            cur.execute("BEGIN IMMEDIATE")
            # fts5vocab resolves genes_fts by name at query time; drop it
            # around the swap so it never points at a half-built index.
            if had_vocab:
                cur.execute("DROP TABLE genes_fts_vocab")
            cur.execute("DROP TABLE genes_fts")  # drops the 8.67GB _content shadow
            cur.execute(FTS_SOURCE_VIEW_SQL)
            cur.execute(FTS_EXTERNAL_CREATE_SQL)
            cur.execute("INSERT INTO genes_fts(genes_fts) VALUES ('rebuild')")
            cur.execute(
                "CREATE VIRTUAL TABLE genes_fts_vocab "
                "USING fts5vocab(genes_fts, instance)"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        report["rebuild_seconds"] = round(time.time() - t0, 2)

        _integrity_check(conn)
        conn.commit()
        report["integrity_check"] = "ok"
        report["fts_rows_after"] = int(
            cur.execute("SELECT COUNT(*) FROM genes_fts_docsize").fetchone()[0]
        )

        # Freed pages sit on the freelist until VACUUM; checkpoint first so
        # the WAL (which just absorbed the rebuild) is folded back in.
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            log.warning("wal_checkpoint(TRUNCATE) failed", exc_info=True)
        if vacuum:
            t0 = time.time()
            conn.execute("VACUUM")
            report["vacuum_seconds"] = round(time.time() - t0, 2)
        report["file_after"] = _file_stats(conn)
        report["status"] = "migrated"
        if vacuum:
            report["message"] = (
                f"migrated + VACUUMed: file "
                f"{report['file_before']['file_bytes'] / 1024 ** 3:.2f} GB -> "
                f"{report['file_after']['file_bytes'] / 1024 ** 3:.2f} GB"
            )
        else:
            report["message"] = (
                f"migrated; {report['file_after']['freelist_bytes'] / 1024 ** 3:.2f} "
                f"GB now on the freelist — run VACUUM (or /admin/vacuum) to "
                f"shrink the file"
            )
        return report
    finally:
        conn.close()


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--db", required=True, help="path to the knowledge store")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the expected reclaim, change nothing")
    ap.add_argument("--vacuum", action="store_true",
                    help="run VACUUM after the rebuild to shrink the file")
    ap.add_argument("--allow-stub-loss", action="store_true",
                    help="proceed even when the store contains compression "
                         "stubs whose original text exists only in the "
                         "contentful FTS shadow (PERMANENTLY LOST on rebuild)")
    ap.add_argument("--out", help="also write the report JSON to this path")
    args = ap.parse_args(argv)

    try:
        report = migrate(
            Path(args.db), dry_run=args.dry_run, vacuum=args.vacuum,
            allow_stub_loss=args.allow_stub_loss,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    if report["status"] == "refused_stub_loss":
        print(f"refused: {report['message']}", file=sys.stderr)
        return 3
    return 0 if report["status"] in ("migrated", "dry_run", "already_migrated") else 1


if __name__ == "__main__":
    raise SystemExit(main())
