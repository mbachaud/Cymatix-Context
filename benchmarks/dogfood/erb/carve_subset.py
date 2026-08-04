"""Carve a first-N-genes subset bed from a full blob genome.

Why carve instead of fresh-ingest: the July ERB full-bed ingest ran across
sessions Jul 2 -> Jul 11 (days of wall time), and a re-ingest bakes today's
tagger/SPLADE settings into the bed — a different instrument. Carving copies
rows from the existing blob, so bed SIZE is the only variable across the
step ladder (docs/benchmarks/2026-08-03-erb-step-curve.md).

The carved bed also gets the 2026-08-03 scale fixes baked in, since a bench
serve opens it read-only and cannot create them lazily:
  - idx_splade_term_cover (term, gene_id, weight)
  - idx_promoter_cover (tag_value, gene_id)
  - splade_term_df counters (exact, computed from the carved postings)
  - sqlite_stat1 via ANALYZE

Usage:
  python benchmarks/dogfood/erb/carve_subset.py \
      --src f:/tmp/erb_blob.db --out f:/tmp/erb_carve/erb_100k.db \
      --genes 100000
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

# Telemetry/log tables: schema is created but rows are not copied — a carved
# bed is a measurement instrument, not a history archive.
SKIP_DATA = {
    "cwola_log", "health_log", "hitl_events", "session_delivery_log",
}
# Pair tables: keep a row only when BOTH endpoints survive the carve.
PAIR_TABLES = {
    "gene_relations": ("gene_id_a", "gene_id_b"),
    "harmonic_links": ("gene_id_a", "gene_id_b"),
}


def log(msg: str) -> None:
    print(f"[carve +{time.perf_counter() - T0:7.1f}s] {msg}", flush=True)


T0 = time.perf_counter()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--genes", type=int, required=True)
    args = ap.parse_args()

    if os.path.exists(args.out):
        print(f"refusing to overwrite existing {args.out}", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    # uri=True on the dest connection also enables URI filenames in ATTACH,
    # which the mode=ro attach of the source relies on.
    dest = sqlite3.connect(f"file:{args.out}", uri=True, timeout=60)
    dest.execute("PRAGMA journal_mode=OFF")
    dest.execute("PRAGMA synchronous=OFF")
    dest.execute("PRAGMA cache_size=-524288")  # 512MB build cache
    dest.execute(f"ATTACH DATABASE 'file:{args.src}?mode=ro' AS src")

    master = dest.execute(
        "SELECT type, name, sql FROM src.sqlite_master "
        "WHERE sql IS NOT NULL ORDER BY rowid"
    ).fetchall()
    tables = [
        (n, s) for t, n, s in master
        if t == "table" and not n.startswith("sqlite_")
        and "VIRTUAL" not in s.upper()
    ]
    virtuals = [(n, s) for t, n, s in master
                if t == "table" and "VIRTUAL" in (s or "").upper()]
    indexes = [(n, s) for t, n, s in master if t == "index"]

    # FTS5 shadow tables are managed by their virtual table — never copy.
    fts_names = {n for n, _ in virtuals}
    shadow = {
        n for n, _ in tables
        if any(n.startswith(f"{f}_") for f in fts_names)
    }

    log(f"schema: {len(tables)} tables ({len(shadow)} fts-shadow skipped), "
        f"{len(virtuals)} virtual, {len(indexes)} indexes")
    for n, s in tables:
        if n not in shadow:
            dest.execute(s)

    # Gene subset boundary: first N by rowid = ingestion order.
    cutoff = dest.execute(
        "SELECT rowid FROM src.genes ORDER BY rowid LIMIT 1 OFFSET ?",
        (args.genes - 1,),
    ).fetchone()
    if cutoff is None:
        print(f"src has fewer than {args.genes} genes", file=sys.stderr)
        return 2
    cutoff = cutoff[0]
    dest.execute(
        "INSERT INTO genes SELECT * FROM src.genes WHERE rowid <= ?",
        (cutoff,),
    )
    n_genes = dest.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
    log(f"genes copied: {n_genes}")

    for name, _ in tables:
        if name in shadow or name == "genes" or name in SKIP_DATA:
            continue
        cols = [r[1] for r in dest.execute(f"PRAGMA src.table_info({name})")]
        if name in PAIR_TABLES:
            a, b = PAIR_TABLES[name]
            dest.execute(
                f"INSERT INTO {name} SELECT t.* FROM src.{name} t "
                f"WHERE t.{a} IN (SELECT gene_id FROM genes) "
                f"AND t.{b} IN (SELECT gene_id FROM genes)"
            )
        elif "gene_id" in cols:
            dest.execute(
                f"INSERT INTO {name} SELECT t.* FROM src.{name} t "
                f"WHERE t.gene_id IN (SELECT gene_id FROM genes)"
            )
        else:
            dest.execute(f"INSERT INTO {name} SELECT * FROM src.{name}")
        n = dest.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        log(f"{name}: {n} rows")
        dest.commit()

    # Virtual tables: create fresh, then rebuild content from carved genes.
    for name, sql in virtuals:
        dest.execute(sql)
    if "genes_fts" in fts_names:
        fts_cols = [
            r[1] for r in dest.execute("PRAGMA table_info(genes_fts)")
        ]
        col_list = ", ".join(fts_cols)
        dest.execute(
            f"INSERT INTO genes_fts({col_list}) "
            f"SELECT {col_list} FROM genes"
        )
        log(f"genes_fts rebuilt over {n_genes} genes: {fts_cols}")
    dest.commit()

    # SPLADE DF counters (2026-08-03 fix): exact for the carved postings.
    dest.execute(
        "CREATE TABLE IF NOT EXISTS splade_term_df "
        "(term TEXT PRIMARY KEY, df INTEGER NOT NULL DEFAULT 0)"
    )
    dest.execute(
        "INSERT INTO splade_term_df "
        "SELECT term, COUNT(*) FROM splade_terms GROUP BY term"
    )
    log("splade_term_df populated")
    dest.commit()

    # Indexes after data (bulk build), source DDL + the two covering ones.
    for name, sql in indexes:
        if any(name.startswith(f"{f}_") for f in fts_names):
            continue
        dest.execute(sql.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1))
        log(f"index {name}")
    dest.execute(
        "CREATE INDEX IF NOT EXISTS idx_splade_term_cover "
        "ON splade_terms (term, gene_id, weight)"
    )
    dest.execute(
        "CREATE INDEX IF NOT EXISTS idx_promoter_cover "
        "ON promoter_index (tag_value, gene_id)"
    )
    log("covering indexes built")
    dest.commit()

    dest.execute("PRAGMA analysis_limit=1000")
    # 'ANALYZE' with no target analyzes ATTACHED dbs too — including the
    # read-only src, which fails. Scope to main.
    dest.execute("ANALYZE main")
    log("ANALYZE done")
    dest.execute("DETACH DATABASE src")
    dest.execute("PRAGMA journal_mode=WAL")
    dest.commit()
    dest.close()
    log(f"DONE: {args.out} ({os.path.getsize(args.out) / 1e9:.1f}GB, "
        f"{n_genes} genes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
