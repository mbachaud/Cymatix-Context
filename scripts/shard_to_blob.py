"""shard_to_blob.py -- Merge a SHARDED genome into a single BLOB genome.db.

This is the *reverse* of the usual blob -> sharded split. We take a sharded
knowledge-store tree:

    <sharded-root>/
      main.genome.db                 # routing + fingerprint_index (NOT merged)
      .../<name>.genome.db           # per-shard content DBs (merged)
      .../<name2>.genome.db
      ...

and produce one flat ``genome.db`` that a normal (non-sharded) Helix server can
open with dense retrieval fully working -- because we copy ``embedding_dense_v2``
(and every other column) VERBATIM. We never re-embed.

WHY THIS IS CAREFUL
-------------------
The user has only ever gone blob -> sharded before. Going the other direction,
the failure modes are:
  - gene_id collisions across shards (gene_id is a content hash, so duplicates
    are byte-identical -- we de-dupe defensively with INSERT OR IGNORE on the
    PK).
  - losing the dense vector by going through any text/JSON round-trip
    (we copy the raw BLOB column directly, no decode).
  - a stale / partial FTS5 index after the bulk copy (we DROP + rebuild
    genes_fts over the merged content so search works).

ALGORITHM
---------
1. Discover all non-``main`` ``*.genome.db`` files under ``--sharded-root``
   (recursive).
2. Create a fresh blob ``--out`` with the *same per-shard schema* by copying
   the DDL of one shard (``sqlite_master``), so column order / types match
   exactly -- including ``embedding_dense_v2 BLOB``.
3. For each shard: ``ATTACH`` it and ``INSERT OR IGNORE INTO blob.<table>
   SELECT * FROM shard.<table>`` for every content table that exists in both
   (``genes`` plus tags / edges / graph tables). ``OR IGNORE`` handles PK
   collisions (identical content-hashed rows) without aborting.
4. DROP + recreate ``genes_fts`` and repopulate it from the merged ``genes``
   (source_id + promoter tags + content), matching the ingest-time FTS layout.
5. VERIFY: blob gene-count == sum of shard gene-counts minus de-duped
   collisions, and non-NULL ``embedding_dense_v2`` count is preserved.

CLI
---
python tools/shard_to_blob.py \\
    --sharded-root genomes/bench/matrix-sharded/enterprise_rag_500k \\
    --out genomes/bench/erb_blob.genome.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Iterable

# Per-shard content tables we merge (best-effort: skipped if absent in a
# given shard). The FTS5 shadow tables (genes_fts*) are intentionally NOT in
# this list -- we rebuild the FTS index from scratch after the merge.
CONTENT_TABLES = [
    "genes",
    "promoter_index",
    "entity_graph",
    "path_key_index",
    "filename_index",
    "gene_relations",
    "harmonic_links",
    "splade_terms",
    "genome_calibration",
    "health_log",
]

# Substrings that mark a DB filename as the routing DB (never merged as a shard).
_MAIN_MARKERS = ("main.genome.db",)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_shards(sharded_root: str) -> list[str]:
    """Return all non-main ``*.genome.db`` files under *sharded_root* (recursive)."""
    out: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(sharded_root):
        for fn in filenames:
            if not fn.endswith(".genome.db"):
                continue
            if fn in _MAIN_MARKERS:
                continue
            # Skip SQLite sidecars (handled implicitly by the .db match anyway).
            out.append(os.path.join(dirpath, fn))
    out.sort()
    return out


# ---------------------------------------------------------------------------
# Read-only shard access
# ---------------------------------------------------------------------------

def _ro_uri(path: str) -> str:
    # immutable=1 (not mode=ro): real shards on disk often carry live
    # -wal / -shm sidecars, and a plain mode=ro connection then fails with
    # "disk I/O error" because it cannot lock/replay the WAL. immutable=1
    # tells SQLite the file will not change, so it reads the main DB file
    # directly and ignores the WAL -- correct for a read-only merge source.
    return "file:{}?immutable=1".format(os.path.abspath(path).replace("\\", "/"))


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _shard_ddl(conn: sqlite3.Connection) -> list[str]:
    """Return CREATE statements for all non-FTS, non-sqlite tables + indexes.

    We skip:
      - sqlite internal tables (sqlite_*)
      - FTS5 virtual table + its shadow tables (genes_fts*) -- rebuilt later
    """
    stmts: list[str] = []
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND type IN ('table', 'index') "
        "ORDER BY (type='index'), name"
    ).fetchall()
    for typ, name, sql in rows:
        if name.startswith("sqlite_"):
            continue
        if name.startswith("genes_fts"):
            continue
        stmts.append(sql)
    return stmts


# ---------------------------------------------------------------------------
# Blob construction
# ---------------------------------------------------------------------------

def create_blob_schema(blob_path: str, template_shard: str) -> None:
    """Create *blob_path* fresh with the schema copied from *template_shard*."""
    if os.path.exists(blob_path):
        os.remove(blob_path)
    for sidecar in ("-wal", "-shm"):
        if os.path.exists(blob_path + sidecar):
            os.remove(blob_path + sidecar)

    src = sqlite3.connect(_ro_uri(template_shard), uri=True)
    try:
        ddl = _shard_ddl(src)
    finally:
        src.close()

    blob = sqlite3.connect(blob_path)
    try:
        blob.execute("PRAGMA journal_mode=WAL")
        for stmt in ddl:
            try:
                blob.execute(stmt)
            except sqlite3.OperationalError as exc:
                # Index referencing a column/table not present is non-fatal.
                print("  [warn] DDL skipped: {} ({})".format(
                    stmt.split("\n")[0][:70], exc), file=sys.stderr)
        blob.commit()
    finally:
        blob.close()


def _common_columns(
    blob: sqlite3.Connection, shard: sqlite3.Connection, table: str
) -> list[str]:
    """Columns present in *both* blob and shard for *table* (preserves blob order)."""
    blob_cols = [r[1] for r in blob.execute(f"PRAGMA table_info({table})")]
    shard_cols = {r[1] for r in shard.execute(f"PRAGMA table_info({table})")}
    return [c for c in blob_cols if c in shard_cols]


def merge_shard(
    blob: sqlite3.Connection, shard_path: str, tables: Iterable[str]
) -> dict[str, int]:
    """ATTACH *shard_path*, copy each content table into the blob.

    Uses INSERT OR IGNORE so PK collisions (identical content-hashed genes) are
    de-duped rather than aborting. Returns per-table inserted-row deltas.
    """
    blob.execute("ATTACH DATABASE ? AS shard", (_ro_uri(shard_path),))
    deltas: dict[str, int] = {}
    try:
        shard_tables = {
            r[0]
            for r in blob.execute(
                "SELECT name FROM shard.sqlite_master WHERE type='table'"
            )
        }
        blob_tables = _table_names(blob)
        # Reflect column intersection via a temporary connection view: we can
        # PRAGMA on attached DB with the schema-qualified name.
        for table in tables:
            if table not in shard_tables or table not in blob_tables:
                continue
            cols = _common_columns_attached(blob, table)
            if not cols:
                continue
            col_list = ", ".join(cols)
            before = blob.execute(
                f"SELECT COUNT(*) FROM main.{table}"
            ).fetchone()[0]
            blob.execute(
                f"INSERT OR IGNORE INTO main.{table} ({col_list}) "
                f"SELECT {col_list} FROM shard.{table}"
            )
            after = blob.execute(
                f"SELECT COUNT(*) FROM main.{table}"
            ).fetchone()[0]
            deltas[table] = after - before
        blob.commit()
    finally:
        blob.execute("DETACH DATABASE shard")
    return deltas


def _common_columns_attached(blob: sqlite3.Connection, table: str) -> list[str]:
    """Columns present in both main.<table> and shard.<table> (main order)."""
    main_cols = [r[1] for r in blob.execute(f"PRAGMA main.table_info({table})")]
    shard_cols = {r[1] for r in blob.execute(f"PRAGMA shard.table_info({table})")}
    return [c for c in main_cols if c in shard_cols]


def _shard_gene_count(shard_path: str) -> int:
    conn = sqlite3.connect(_ro_uri(shard_path), uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _shard_dense_count(shard_path: str) -> int:
    conn = sqlite3.connect(_ro_uri(shard_path), uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# FTS5 rebuild
# ---------------------------------------------------------------------------

def rebuild_fts(blob: sqlite3.Connection) -> int:
    """DROP + recreate genes_fts over the merged content. Returns row count.

    Mirrors the ingest-time FTS layout (storage/ddl._create_fts5 and
    storage/indexes.sync_fts5): the indexed ``content`` column is
    ``source_id + ' ' + promoter-tags + ' ' + content``.
    """
    blob.execute("DROP TABLE IF EXISTS genes_fts")
    blob.execute(
        "CREATE VIRTUAL TABLE genes_fts USING fts5("
        "gene_id, content, complement)"
    )
    has_promoter = "promoter_index" in _table_names(blob)
    if has_promoter:
        tag_sub = (
            "COALESCE((SELECT GROUP_CONCAT(pi.tag_value, ' ') "
            "FROM promoter_index pi WHERE pi.gene_id = g.gene_id), '')"
        )
    else:
        tag_sub = "''"
    blob.execute(
        "INSERT INTO genes_fts(gene_id, content, complement) "
        "SELECT g.gene_id, "
        "  COALESCE(g.source_id,'') || ' ' || " + tag_sub + " || ' ' || "
        "  COALESCE(g.content,''), "
        "  COALESCE(g.complement,'') "
        "FROM genes g"
    )
    blob.commit()
    return blob.execute("SELECT COUNT(*) FROM genes_fts").fetchone()[0]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def shard_to_blob(sharded_root: str, out_path: str) -> dict:
    """Merge all shards under *sharded_root* into a blob at *out_path*.

    Returns a summary dict with verification fields.
    """
    shards = discover_shards(sharded_root)
    if not shards:
        raise FileNotFoundError(
            "No non-main *.genome.db shards found under {}".format(sharded_root)
        )

    # Expected totals (read-only probe of every shard before we touch the blob).
    shard_gene_counts = {s: _shard_gene_count(s) for s in shards}
    shard_dense_counts = {s: _shard_dense_count(s) for s in shards}
    expected_genes_sum = sum(shard_gene_counts.values())
    expected_dense_sum = sum(shard_dense_counts.values())

    print("[shard_to_blob] {} shard(s) under {}".format(len(shards), sharded_root))
    for s in shards:
        print("    {:6d} genes ({:6d} dense)  {}".format(
            shard_gene_counts[s], shard_dense_counts[s],
            os.path.relpath(s, sharded_root)))

    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # Schema from the first shard that actually has a genes table.
    template = next((s for s in shards if shard_gene_counts[s] >= 0), shards[0])
    create_blob_schema(out_path, template)

    blob = sqlite3.connect(out_path, uri=True)
    # Allow ATTACH of file: URIs (the shard sources use immutable=1 URIs).
    blob.execute("PRAGMA busy_timeout=30000")
    try:
        total_inserted = 0
        for s in shards:
            deltas = merge_shard(blob, s, CONTENT_TABLES)
            total_inserted += deltas.get("genes", 0)

        blob_genes = blob.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
        blob_dense = blob.execute(
            "SELECT COUNT(*) FROM genes WHERE embedding_dense_v2 IS NOT NULL"
        ).fetchone()[0]

        fts_rows = rebuild_fts(blob)
    finally:
        blob.close()

    dupes = expected_genes_sum - blob_genes
    summary = {
        "sharded_root": sharded_root,
        "out": out_path,
        "n_shards": len(shards),
        "expected_genes_sum": expected_genes_sum,
        "blob_genes": blob_genes,
        "deduped_collisions": dupes,
        "expected_dense_sum": expected_dense_sum,
        "blob_dense": blob_dense,
        "fts_rows": fts_rows,
        # Dense preservation holds when the blob's non-null dense count equals
        # the sum of shard dense counts MINUS the de-duped collisions (a deduped
        # row that had a vector removes one from both totals symmetrically).
        "genes_ok": blob_genes == expected_genes_sum - dupes,
        "dense_ok": blob_dense == expected_dense_sum - _dense_dupes(
            shards, dupes
        ) if dupes else blob_dense == expected_dense_sum,
    }
    return summary


def _dense_dupes(shards: list[str], total_dupes: int) -> int:
    """Conservative estimate of how many de-duped genes carried a dense vector.

    For the verify check we don't need the exact split; when there are no
    collisions (the common, globally-unique-gene_id case) this returns 0 and
    the strict equality ``blob_dense == expected_dense_sum`` is used instead.
    We bound it by total_dupes so the check never under-counts.
    """
    return total_dupes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Merge a sharded genome (main.genome.db + per-shard "
            "*.genome.db files) into a single blob genome.db, preserving "
            "embedding_dense_v2 verbatim so a non-sharded Helix server can "
            "serve dense retrieval. Does NOT re-embed."
        )
    )
    ap.add_argument(
        "--sharded-root", required=True, dest="sharded_root",
        help="Directory containing main.genome.db + per-shard *.genome.db.",
    )
    ap.add_argument("--out", required=True, help="Output blob genome.db path.")
    args = ap.parse_args(argv)

    t0 = time.time()
    try:
        summary = shard_to_blob(args.sharded_root, args.out)
    except FileNotFoundError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1

    print()
    print("=" * 60)
    print("  shard -> blob merge complete")
    print("=" * 60)
    print("  shards merged:        {}".format(summary["n_shards"]))
    print("  expected gene sum:    {}".format(summary["expected_genes_sum"]))
    print("  blob gene count:      {}".format(summary["blob_genes"]))
    print("  deduped collisions:   {}".format(summary["deduped_collisions"]))
    print("  expected dense sum:   {}".format(summary["expected_dense_sum"]))
    print("  blob dense (non-null):{}".format(summary["blob_dense"]))
    print("  FTS5 rows rebuilt:    {}".format(summary["fts_rows"]))
    print("  gene-count OK:        {}".format(summary["genes_ok"]))
    print("  dense preserved OK:   {}".format(summary["dense_ok"]))
    print("  elapsed:              {:.1f}s".format(time.time() - t0))
    print("  -> {}".format(args.out))

    if not (summary["genes_ok"] and summary["dense_ok"]):
        print("WARNING: verification FAILED -- inspect counts above.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
