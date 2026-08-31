"""Add the COVER-walk reverse adjacency index to an existing bed (W2.1).

New beds get ``idx_gene_relations_rel_b`` from ``storage/ddl.py``; this
script retrofits it onto beds built before 2026-08-28. Idempotent — a bed
that already has the index is a no-op. Prints bytes before/after so the
campaign row / receipt can record the bed mutation (phase sequencing rule 5:
migrations run BETWEEN campaigns and the new bed_bytes is recorded).

Usage:
  python scripts/add_gene_relations_reverse_index.py --db F:/tmp/erb_blob_v09x.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

INDEX_NAME = "idx_gene_relations_rel_b"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="bed file to migrate")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: {db} does not exist", file=sys.stderr)
        return 2

    before = db.stat().st_size
    conn = sqlite3.connect(str(db))
    try:
        have = {
            r[1] for r in conn.execute("PRAGMA index_list('gene_relations')")
        }
        if INDEX_NAME in have:
            print(f"{INDEX_NAME} already present — no-op")
            print(f"bytes: {before}")
            return 0
        n = conn.execute("SELECT COUNT(*) FROM gene_relations").fetchone()[0]
        print(f"building {INDEX_NAME} over {n} gene_relations rows...")
        t0 = time.time()
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
            "ON gene_relations(relation, gene_id_b)"
        )
        conn.commit()
        print(f"built in {time.time() - t0:.1f}s")
    finally:
        conn.close()
    after = db.stat().st_size
    print(f"bytes before: {before}")
    print(f"bytes after:  {after} (+{after - before})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
