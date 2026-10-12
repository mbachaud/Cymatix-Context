"""
Opt-in migration: build the genes(gene_id, chromatin) covering index.

perf/tier-sql (PR #508): the tag tiers probe ``genes`` once per matched gene
only to test ``chromatin < 2``.  ``idx_genes_id_chromatin`` answers that from a
small hot b-tree.  It is NOT created when a store opens: on a 947k store the
build takes ~38 s and adds ~24.8 MB, and it holds the database WRITE LOCK
while building (readers continue under WAL, writers wait).  Query results are
identical with or without it.

Idempotent and safe to re-run (``CREATE INDEX IF NOT EXISTS``; reports
``already_present`` when there is nothing to do).  Stop other writers first.

Usage:
    python scripts/migrate_genes_cover_index.py --db genomes/main/genome.db --dry-run
    python scripts/migrate_genes_cover_index.py --db genomes/main/genome.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union


def _ensure_root_on_path():
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_ensure_root_on_path()

from cymatix_context.storage.ddl import (  # noqa: E402
    GENES_COVER_INDEX,
    GENES_COVER_INDEX_SQL,
)


def migrate(db_path: Union[str, Path], dry_run: bool = False) -> Dict[str, Any]:
    """Create the covering index on *db_path*.

    ``status``: ``created`` / ``already_present`` / ``dry_run``.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"knowledge store not found: {db_path}")
    report: Dict[str, Any] = {
        "tool": "scripts/migrate_genes_cover_index.py",
        "db": str(db_path),
        "index": GENES_COVER_INDEX,
        "dry_run": dry_run,
        "holds_write_lock": True,
    }
    conn = sqlite3.connect(str(db_path), timeout=60)
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (GENES_COVER_INDEX,),
        ).fetchone() is not None
        report["max_rowid"] = conn.execute(
            "SELECT MAX(rowid) FROM genes").fetchone()[0] or 0
        if present:
            report.update(status="already_present", elapsed_s=0.0)
            return report
        if dry_run:
            report.update(status="dry_run", elapsed_s=0.0)
            return report
        t0 = time.perf_counter()
        conn.execute(GENES_COVER_INDEX_SQL)
        conn.commit()
        report.update(status="created",
                      elapsed_s=round(time.perf_counter() - t0, 3))
        return report
    finally:
        conn.close()


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--db", required=True, help="path to the knowledge store")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen, change nothing")
    args = ap.parse_args(argv)
    try:
        report = migrate(Path(args.db), dry_run=args.dry_run)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
