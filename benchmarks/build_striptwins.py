"""Build a SPLADE strip-twin bed pair for the #204 scale curve.

Mechanism confirmed cheap by the 2026-07-11 overnight rig (P9,
``docs/research/2026-07-11-overnight-bench-results.md``): copy a
SPLADE-on bed twice, then on one copy ``DROP TABLE splade_terms`` +
``VACUUM``. That "off" twin has byte-identical genes/tags/FTS5/dense
vectors to the "on" twin -- the only difference is the absence of the
SPLADE inverted index -- so an on/off comparison isolates SPLADE's
retrieval contribution without a multi-hour SPLADE-off ingest rebuild.

Usage:
    python benchmarks/build_striptwins.py <label> <source_db> <out_dir>

Writes ``<out_dir>/<label>_on.db`` (plain copy of ``source_db``) and
``<out_dir>/<label>_off.db`` (splade_terms dropped + vacuumed). Prints a
JSON receipt (gene/row counts, before/after bytes, disk delta) to
stdout. Feed both paths to ``sweep_splade_scale_curve.py``'s
``--on-genome`` / ``--off-genome`` single-pair mode.

Source beds are read-only (only ``shutil.copyfile`` touches them); all
writes land on the two new copies under ``out_dir``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time


def size_of(path: str) -> int:
    total = os.path.getsize(path)
    for suffix in ("-wal", "-shm"):
        s = path + suffix
        if os.path.exists(s):
            total += os.path.getsize(s)
    return total


def build_striptwin(label: str, source_db: str, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    on_path = os.path.join(out_dir, f"{label}_on.db")
    off_path = os.path.join(out_dir, f"{label}_off.db")

    t0 = time.monotonic()
    shutil.copyfile(source_db, on_path)
    on_bytes = size_of(on_path)

    shutil.copyfile(source_db, off_path)

    conn = sqlite3.connect(off_path)
    try:
        splade_rows = conn.execute("SELECT COUNT(*) FROM splade_terms").fetchone()[0]
        gene_count = conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        conn.execute("DROP TABLE splade_terms")
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    off_bytes = size_of(off_path)
    elapsed = time.monotonic() - t0

    return {
        "label": label,
        "source_db": source_db,
        "on_db": on_path,
        "off_db": off_path,
        "gene_count": gene_count,
        "splade_rows_dropped": splade_rows,
        "on_bytes": on_bytes,
        "off_bytes_after_strip": off_bytes,
        "bytes_per_gene_on": round(on_bytes / gene_count, 1),
        "bytes_per_gene_off": round(off_bytes / gene_count, 1),
        "disk_delta_bytes_per_gene": round((on_bytes - off_bytes) / gene_count, 1),
        "disk_pct_overhead": round(100.0 * (on_bytes - off_bytes) / off_bytes, 2),
        "build_elapsed_s": round(elapsed, 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("label", help="Scale-point label, e.g. erag10k")
    ap.add_argument("source_db", help="Path to the SPLADE-on canonical bed (read-only)")
    ap.add_argument("out_dir", help="Scratch directory for the two twin copies")
    args = ap.parse_args(argv)

    receipt = build_striptwin(args.label, args.source_db, args.out_dir)
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
