"""rebuild_fts_arm.py — copy a bed and rebuild its FTS index per arm.

The 2026-07-31 tag-flood analysis identified two channels carrying tag
noise into retrieval: the Tier-1 tag tier (addressed by
``[retrieval] tag_df_cap``) and the FTS index itself — tag text is baked
into the ``genes_fts.content`` column at index time
(``storage/indexes.py:76-88`` per-doc; ``storage/ddl.py:426-436`` bulk).
Tier pruning cannot reach the second channel; measuring it needs an FTS
rebuild per arm.

This tool copies a bed via the SQLite backup API (the source bed is a
receipt and is never touched) and rebuilds ``genes_fts`` from the genes
table, mirroring the bulk-backfill composition exactly except for the
tag term:

    --tags stock         source_id + all tags + content   (shipped)
    --tags capped:0.02   tags with df > 2% of corpus excluded
    --tags none          source_id + content only          (full bracket)

The complement column is preserved untouched in every mode.

Usage::

    python benchmarks/dogfood/rebuild_fts_arm.py \
        --src genomes/taggerdogfood_cpu/genome.db \
        --out genomes/_ftsarms/fts_none.db --tags none
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tags", default="stock",
                    help='"stock" | "none" | "capped:<frac>"')
    args = ap.parse_args()

    src = REPO_ROOT / args.src
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    t0 = time.time()
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as sconn, \
            sqlite3.connect(out) as dconn:
        sconn.backup(dconn)
    print(f"copied {src.name} -> {out} ({time.time()-t0:.0f}s)")

    conn = sqlite3.connect(out)
    cur = conn.cursor()
    n_genes = cur.execute("SELECT COUNT(*) FROM genes").fetchone()[0]

    excluded: list[str] = []
    tag_subquery = ("COALESCE((SELECT GROUP_CONCAT(pi.tag_value, ' ') "
                    "FROM promoter_index pi WHERE pi.gene_id = g.gene_id), '')")
    if args.tags == "none":
        tag_subquery = "''"
    elif args.tags.startswith("capped:"):
        cap = float(args.tags.split(":", 1)[1])
        rows = cur.execute(
            "SELECT tag_value, COUNT(DISTINCT gene_id) AS df "
            "FROM promoter_index GROUP BY tag_value"
        ).fetchall()
        excluded = sorted(t for t, df in rows if df / n_genes > cap)
        if excluded:
            ph = ",".join("?" * len(excluded))
            tag_subquery = (
                f"COALESCE((SELECT GROUP_CONCAT(pi.tag_value, ' ') "
                f"FROM promoter_index pi WHERE pi.gene_id = g.gene_id "
                f"AND pi.tag_value NOT IN ({ph})), '')"
            )
    elif args.tags != "stock":
        print(f"ERROR: unknown --tags {args.tags!r}", file=sys.stderr)
        return 2

    t0 = time.time()
    # 'delete-all' needs a contentless/external-content table; genes_fts
    # is a plain fts5 table, so row-wise DELETE is the supported path.
    cur.execute("DELETE FROM genes_fts")
    cur.execute(
        "INSERT INTO genes_fts(gene_id, content, complement) "
        "SELECT g.gene_id, "
        f"  COALESCE(g.source_id,'') || ' ' || {tag_subquery} "
        "  || ' ' || g.content, "
        "  COALESCE(g.complement, '') "
        "FROM genes g",
        tuple(excluded) if excluded and args.tags.startswith("capped:") else (),
    )
    conn.commit()
    fts_rows = cur.execute("SELECT COUNT(*) FROM genes_fts").fetchone()[0]
    conn.close()

    print(f"rebuilt genes_fts: {fts_rows}/{n_genes} rows, mode={args.tags}, "
          f"tags_excluded={len(excluded)} ({time.time()-t0:.0f}s)")
    if excluded:
        print(f"  excluded (df-desc sample): {excluded[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
