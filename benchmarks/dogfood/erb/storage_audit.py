"""storage_audit.py — per-retrieval-layer disk accounting for a cymatix bed.

The cost half of the cost/impact re-probe: ``ablation_ladder.py`` measures what
each retrieval layer buys, this measures what it costs on disk. #164's
enterprise finding ("SPLADE was 21.1% of disk for 0 pp recall@10 at 850K") is
exactly the shape of claim this tool has to be able to make — or refute — on
any bed, without hand-counting pages.

Two modes:

``--exact``   Walk the ``dbstat`` virtual table, one row per b-tree page,
              aggregated per object::

                  SELECT name, sum(pgsize) FROM dbstat GROUP BY name

              This is the truth: real page occupancy including free space and
              overflow. It is a full-file scan, which is fine on beds up to a
              few GB. NOTE: ``dbstat`` needs SQLITE_ENABLE_DBSTAT_VTAB in the
              sqlite build. The stock CPython on this box does NOT have it
              (verified 2026-08-05: "no such table: dbstat"), so ``--exact``
              degrades to the estimator with ``exact_available: false`` and the
              error recorded, rather than dying or — worse — silently pretending
              the estimate was exact.

default       Estimate. Per object: row count x sampled mean row bytes x 1.3
              b-tree overhead. The sample is the first 10,000 rows; byte
              lengths use ``length(CAST(col AS BLOB))`` so a UTF-8 text column
              is measured in bytes, not codepoints. Index sizes estimate the
              indexed columns plus an 8-byte rowid per entry. The FILE total is
              never estimated — it is exact page math
              (``page_size x page_count``) — so the receipt always carries a
              hard denominator and an explicit ``unaccounted_bytes`` residual.

Layer mapping is hardcoded, derived from the actual schema of the ERB carve
beds (verified against f:/tmp/erb_carve/erb_100k.db, 2026-08-05). Anything the
map does not recognise lands in ``unknown`` and is listed by name — never
folded into a bucket to make the percentages look tidy.

Two column-level splits inside ``genes`` are made because the interesting
layers live in columns, not tables:
  * ``genes.embedding_dense_v2`` -> layer ``dense``
  * ``genes.embedding``          -> layer ``sema``
Both are estimated as (mean non-null blob bytes x non-null count) and are
SUBTRACTED from the ``genes``/``core`` bucket so they are not double counted.
In ``--exact`` mode the ``genes`` b-tree total is exact and the two column
estimates are carved out of it; that carve-out is flagged
``column_split: "estimated"`` in the output because dbstat cannot attribute
pages to columns.

Usage::

    python benchmarks/dogfood/erb/storage_audit.py \
        --db f:/tmp/erb_carve/erb_100k.db \
        --out benchmarks/dogfood/erb/receipts/storage_audit_100k.json
    python benchmarks/dogfood/erb/storage_audit.py --db ... --exact --out ...
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Sampled rows per object for the estimator. 10k is enough to stabilise the
# mean row size on every bed measured so far and stays sub-second.
SAMPLE_ROWS = 10_000
# B-tree overhead multiplier: cell headers, per-page slack, interior pages.
BTREE_OVERHEAD = 1.3
# Rowid bytes charged per index entry.
INDEX_ROWID_BYTES = 8

# ── Layer map ───────────────────────────────────────────────────────────
#
# Ordered (pattern, layer, rationale). FIRST match wins, so the specific
# patterns must precede the general ones. Derived from the ERB carve schema;
# each rationale names the code that consumes the object so a reader can check
# the attribution instead of trusting it.
LAYER_PATTERNS: Tuple[Tuple[str, str, str], ...] = (
    # FTS5 external-content shadow tables.
    (r"^genes_fts(_|$)", "fts5",
     "FTS5 shadow tables for Tier 3 content search (knowledge_store.py:2780)"),
    # SPLADE sparse expansion.
    (r"^splade_term", "splade",
     "SPLADE postings + df counters, Tier 3.5 (knowledge_store.py:2783-2842)"),
    (r"^idx_splade", "splade", "SPLADE posting-list covering indexes"),
    (r"^sqlite_autoindex_splade", "splade", "SPLADE table UNIQUE autoindex"),
    # Tag tiers. The bed has no `tags` table: the tag tiers join
    # promoter_index (knowledge_store.py:2676 tag_exact, :2707 tag_prefix).
    (r"^promoter_index$", "tags",
     "Tier 1/2 tag_exact + tag_prefix join this table (knowledge_store.py:2676, :2707)"),
    (r"^idx_promoter", "tags", "tag-tier covering indexes"),
    # Dense (BGE-M3) — the hot-id index; the column itself is handled separately.
    (r"^idx_genes_dense_v2_hot$", "dense",
     "hot-tier partial index over embedding_dense_v2 (knowledge_store dense recall)"),
    # Co-activation / harmonic graph.
    (r"^harmonic_links$", "coact",
     "Tier 5 harmonic boost (knowledge_store.py:3119) + retrieval/expand.py"),
    (r"^idx_harmonic", "coact", "harmonic_links traversal indexes"),
    (r"^sqlite_autoindex_harmonic_links", "coact", "harmonic_links PK autoindex"),
    (r"^gene_relations$", "coact",
     "document-linkage edges, retrieval-live per okf/__init__.py:10"),
    (r"^idx_gene_relations", "coact", "gene_relations traversal index"),
    (r"^sqlite_autoindex_gene_relations", "coact", "gene_relations PK autoindex"),
    # Path/key index — the PKI tier.
    (r"^path_key_index$", "pki", "PKI tier (knowledge_store.py:2552 _sig('pki'))"),
    (r"^idx_pki", "pki", "PKI lookup indexes"),
    # Tier 0.5 filename anchor.
    (r"^filename_index$", "filename_anchor",
     "Tier 0.5 filename anchor ([retrieval] filename_anchor_enabled)"),
    (r"^idx_filename_stem$", "filename_anchor", "filename anchor lookup index"),
    (r"^sqlite_autoindex_filename_index", "filename_anchor", "filename_index PK autoindex"),
    # Entity graph (Tier 5b, dark-shipped).
    (r"^entity_graph$", "entity_graph",
     "Tier 5b entity co-occurrence ([retrieval] entity_graph_retrieval_enabled)"),
    (r"^idx_entity_graph", "entity_graph", "entity_graph lookup indexes"),
    (r"^sqlite_autoindex_entity_graph", "entity_graph", "entity_graph PK autoindex"),
    # Symbol graph (WS2, default off).
    (r"^symbol_defs$", "symbol_graph", "WS2 symbol definitions ([ingestion] symbol_graph)"),
    (r"^idx_symbol_defs", "symbol_graph", "symbol_defs lookup indexes"),
    (r"^sqlite_autoindex_symbol_defs", "symbol_graph", "symbol_defs PK autoindex"),
    # Identity / attribution (retrieval-adjacent: party filtering).
    (r"^(agents|participants|parties|orgs|gene_attribution)$", "identity",
     "party/agent attribution — feeds the cross-party retrieval filter"),
    (r"^idx_(agents|participants|parties|orgs|attribution)", "identity",
     "identity lookup indexes"),
    (r"^sqlite_autoindex_(agents|participants|parties|orgs|gene_attribution)",
     "identity", "identity PK/UNIQUE autoindexes"),
    # Ops / telemetry — NOT retrieval.
    (r"^(cwola_log|health_log|hitl_events|session_delivery_log)$", "ops_not_retrieval",
     "logging + session bookkeeping; carries no retrieval signal"),
    (r"^idx_(cwola|sdl|hitl)", "ops_not_retrieval", "ops log indexes"),
    (r"^sqlite_autoindex_hitl_events", "ops_not_retrieval", "hitl_events PK autoindex"),
    # OKF bundles.
    (r"^okf_links$", "okf", "OKF bundle links (okf/ subsystem)"),
    (r"^idx_okf_links", "okf", "OKF bundle index"),
    # Core document store + sqlite internals.
    (r"^genes$", "core", "the document rows themselves"),
    (r"^idx_genes_", "core", "genes housekeeping indexes (last_seen, supersedes)"),
    (r"^sqlite_autoindex_genes", "core", "genes PK autoindex"),
    (r"^genome_calibration$", "core", "persisted ANN threshold calibration"),
    (r"^sqlite_autoindex_genome_calibration", "core", "genome_calibration PK autoindex"),
    (r"^(sqlite_sequence|sqlite_stat1)$", "core", "SQLite internal bookkeeping"),
)

# Columns carved out of `genes` into their own layer.
COLUMN_LAYERS: Tuple[Tuple[str, str, str], ...] = (
    ("embedding_dense_v2", "dense",
     "BGE-M3 dense vectors ([retrieval] dense_embedding_enabled)"),
    ("embedding", "sema",
     "20D SEMA vectors ([ingestion] sema_embed_on_ingest / Tier 4 + cymatics)"),
)


def classify_object(name: str) -> Tuple[str, str]:
    """Map a sqlite_master object name to ``(layer, rationale)``.

    Unrecognised objects return ``("unknown", ...)`` so they surface in the
    receipt by name instead of quietly inflating a bucket.
    """
    for pattern, layer, rationale in LAYER_PATTERNS:
        if re.search(pattern, name):
            return layer, rationale
    return "unknown", "no layer mapping — review and add to LAYER_PATTERNS"


def to_gb(num_bytes: float) -> float:
    return round(num_bytes / (1024 ** 3), 6)


def file_page_math(conn: sqlite3.Connection) -> Dict[str, int]:
    """Exact file size from page math. Never estimated."""
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


def dbstat_sizes(conn: sqlite3.Connection) -> Tuple[Optional[Dict[str, int]], Optional[str]]:
    """Exact per-object bytes from ``dbstat``. Returns ``(sizes, error)``."""
    try:
        rows = conn.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
        ).fetchall()
    except sqlite3.Error as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return {str(name): int(size or 0) for name, size in rows}, None


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.Error:
        return 0


def sampled_mean_row_bytes(
    conn: sqlite3.Connection, table: str, columns: Sequence[str],
) -> float:
    """Mean bytes/row over the first ``SAMPLE_ROWS`` rows.

    ``CAST(col AS BLOB)`` makes ``length`` return BYTES for text columns
    (``length('é')`` is 1 as text, 2 as blob) — an audit that reports
    codepoints as bytes understates every UTF-8 corpus.
    """
    if not columns:
        return 0.0
    expr = " + ".join(
        f'COALESCE(LENGTH(CAST("{c}" AS BLOB)), 0)' for c in columns
    )
    try:
        row = conn.execute(
            f'SELECT AVG(w) FROM (SELECT ({expr}) AS w FROM "{table}" '
            f"LIMIT {SAMPLE_ROWS})"
        ).fetchone()
    except sqlite3.Error:
        return 0.0
    return float(row[0] or 0.0)


def column_bytes(conn: sqlite3.Connection, table: str, column: str) -> Tuple[int, int]:
    """``(estimated_total_bytes, non_null_rows)`` for one column.

    Mean length is sampled over the first ``SAMPLE_ROWS`` NON-NULL values and
    multiplied by the exact non-null count — the blob columns here are fixed
    width per model, so the sample is tight.
    """
    try:
        n_rows = int(conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NOT NULL'
        ).fetchone()[0])
    except sqlite3.Error:
        return 0, 0
    if not n_rows:
        return 0, 0
    row = conn.execute(
        f'SELECT AVG(w) FROM (SELECT LENGTH(CAST("{column}" AS BLOB)) AS w '
        f'FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT {SAMPLE_ROWS})'
    ).fetchone()
    return int(round(float(row[0] or 0.0) * n_rows)), n_rows


def estimate_index_bytes(conn: sqlite3.Connection, index: str) -> int:
    """Estimate one index: (indexed column bytes + rowid) x rows x overhead."""
    try:
        info = conn.execute(f'PRAGMA index_info("{index}")').fetchall()
        table_row = conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE name = ?", (index,)
        ).fetchone()
    except sqlite3.Error:
        return 0
    if not table_row:
        return 0
    table = str(table_row[0])
    cols = [str(r[2]) for r in info if r[2] is not None]
    n_rows = _row_count(conn, table)
    if not n_rows:
        return 0
    per_row = sampled_mean_row_bytes(conn, table, cols) + INDEX_ROWID_BYTES
    return int(round(per_row * n_rows * BTREE_OVERHEAD))


def estimate_table_bytes(conn: sqlite3.Connection, table: str) -> Tuple[int, int]:
    """``(estimated_bytes, row_count)`` for one table."""
    n_rows = _row_count(conn, table)
    if not n_rows:
        return 0, 0
    cols = _table_columns(conn, table)
    per_row = sampled_mean_row_bytes(conn, table, cols)
    return int(round(per_row * n_rows * BTREE_OVERHEAD)), n_rows


def virtual_tables(conn: sqlite3.Connection) -> set:
    """Names of VIRTUAL tables — objects that own no b-tree of their own.

    ``genes_fts`` (fts5) and ``genes_fts_vocab`` (fts5vocab) both answer
    ``SELECT COUNT(*)`` and expose readable columns, so a naive estimator
    charges them for storage that actually lives in their shadow tables
    (``genes_fts_data`` / ``_content`` / ``_idx`` / ``_docsize`` / ``_config``).
    On the 100K ERB carve that double-count was 2.86 GB against a 5.99 GB file
    — nearly half the bed invented. dbstat returns no rows for them either, so
    zeroing them keeps both modes consistent.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND sql LIKE 'CREATE VIRTUAL TABLE%'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def list_objects(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """``[(type, name)]`` for every table/index, including sqlite_ internals."""
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table','index') "
        "ORDER BY type, name"
    ).fetchall()
    names = {(str(t), str(n)) for t, n in rows}
    # sqlite_master does not list these; they hold real pages.
    for internal in ("sqlite_sequence", "sqlite_stat1"):
        try:
            conn.execute(f"SELECT 1 FROM {internal} LIMIT 1")
            names.add(("table", internal))
        except sqlite3.Error:
            pass
    return sorted(names, key=lambda x: (x[0], x[1]))


def audit(db_path: Path, want_exact: bool) -> Dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        pages = file_page_math(conn)
        exact_sizes: Optional[Dict[str, int]] = None
        exact_error: Optional[str] = None
        if want_exact:
            exact_sizes, exact_error = dbstat_sizes(conn)
        mode = "exact(dbstat)" if exact_sizes else "estimate"
        if want_exact and exact_sizes is None:
            mode = "estimate (dbstat unavailable — see exact_error)"

        virtual = virtual_tables(conn)
        objects: List[Dict[str, Any]] = []
        for obj_type, name in list_objects(conn):
            layer, rationale = classify_object(name)
            if name in virtual:
                size = 0
                measured = "virtual (no own b-tree; storage is in its shadow tables)"
            elif exact_sizes is not None:
                size = int(exact_sizes.get(name, 0))
                measured = "exact"
            elif obj_type == "table":
                size, _ = estimate_table_bytes(conn, name)
                measured = "estimated"
            else:
                size = estimate_index_bytes(conn, name)
                measured = "estimated"
            entry: Dict[str, Any] = {
                "name": name,
                "type": obj_type,
                "layer": layer,
                "rationale": rationale,
                "bytes": size,
                "gb": to_gb(size),
                "measured": measured,
            }
            if obj_type == "table":
                entry["rows"] = _row_count(conn, name)
            objects.append(entry)

        # Column carve-out: pull the dense/sema blob columns out of `genes`.
        genes_cols = set(_table_columns(conn, "genes"))
        column_entries: List[Dict[str, Any]] = []
        carved_total = 0
        for column, layer, rationale in COLUMN_LAYERS:
            if column not in genes_cols:
                continue
            size, non_null = column_bytes(conn, "genes", column)
            carved_total += size
            column_entries.append({
                "name": f"genes.{column}",
                "type": "column",
                "layer": layer,
                "rationale": rationale,
                "bytes": size,
                "gb": to_gb(size),
                "measured": "estimated",
                "non_null_rows": non_null,
            })

        genes_entry = next((o for o in objects if o["name"] == "genes"), None)
        column_split_note = None
        if genes_entry is not None and carved_total:
            # Never let the carve-out drive `core` negative: if the blob
            # estimate exceeds the measured b-tree size, report the clamp
            # loudly rather than emitting a nonsense negative bucket.
            if carved_total >= genes_entry["bytes"]:
                column_split_note = (
                    f"column carve-out ({carved_total} B) met or exceeded the "
                    f"measured genes b-tree ({genes_entry['bytes']} B); core "
                    "clamped to 0 — treat the dense/sema column figures as "
                    "upper bounds on this bed"
                )
                genes_entry["bytes_after_column_carveout"] = 0
            else:
                genes_entry["bytes_after_column_carveout"] = (
                    genes_entry["bytes"] - carved_total
                )
                column_split_note = (
                    "genes.embedding_dense_v2 / genes.embedding are estimated "
                    "(sampled mean blob length x non-null count) and subtracted "
                    "from the genes b-tree; dbstat cannot attribute pages to "
                    "columns, so this split is estimated even in --exact mode"
                )
            genes_entry["column_split"] = "estimated"

        # Roll up.
        by_layer: Dict[str, Dict[str, Any]] = {}

        def _add(layer: str, size: int, name: str) -> None:
            slot = by_layer.setdefault(layer, {"bytes": 0, "objects": []})
            slot["bytes"] += size
            slot["objects"].append(name)

        for obj in objects:
            size = obj.get("bytes_after_column_carveout", obj["bytes"])
            _add(obj["layer"], int(size), obj["name"])
        for col in column_entries:
            _add(col["layer"], int(col["bytes"]), col["name"])

        accounted = sum(v["bytes"] for v in by_layer.values())
        file_bytes = pages["file_bytes"]
        layers_out = {
            layer: {
                "bytes": int(vals["bytes"]),
                "gb": to_gb(vals["bytes"]),
                "pct_of_file": (round(100.0 * vals["bytes"] / file_bytes, 2)
                                if file_bytes else None),
                "objects": sorted(vals["objects"]),
            }
            for layer, vals in sorted(
                by_layer.items(), key=lambda kv: -kv[1]["bytes"]
            )
        }

        unknown = sorted(
            o["name"] for o in objects if o["layer"] == "unknown"
        )

        return {
            "tool": "benchmarks/dogfood/erb/storage_audit.py",
            "db": str(db_path),
            "mode": mode,
            "exact_requested": want_exact,
            "exact_available": exact_sizes is not None,
            "exact_error": exact_error,
            "estimator": {
                "sample_rows": SAMPLE_ROWS,
                "btree_overhead": BTREE_OVERHEAD,
                "index_rowid_bytes": INDEX_ROWID_BYTES,
                "note": (
                    "rows x sampled mean row bytes x btree_overhead; byte "
                    "lengths via LENGTH(CAST(col AS BLOB)) so UTF-8 text is "
                    "counted in bytes, not codepoints"
                ),
            },
            "file": {
                **pages,
                "file_gb": to_gb(file_bytes),
                "on_disk_bytes": db_path.stat().st_size,
            },
            "accounted_bytes": accounted,
            "accounted_gb": to_gb(accounted),
            "accounted_pct_of_file": (round(100.0 * accounted / file_bytes, 2)
                                      if file_bytes else None),
            "unaccounted_bytes": file_bytes - accounted,
            "unaccounted_gb": to_gb(file_bytes - accounted),
            "unaccounted_note": (
                "file_bytes minus the sum of per-object sizes. In exact mode "
                "this is free/unused pages only. In estimate mode it also "
                "absorbs estimator error, and the SIGN matters: positive means "
                "the estimator under-counted (the layers are bigger than "
                "listed), negative means it over-counted (the btree_overhead "
                "multiplier and the per-index column estimates are the usual "
                "culprits). Per-layer percentages are shares of the exact file "
                "size, so they do not sum to 100 when the residual is nonzero."
            ),
            "column_split_note": column_split_note,
            "layers": layers_out,
            "unknown_objects": unknown,
            "objects": sorted(
                objects + column_entries, key=lambda o: -o["bytes"]
            ),
        }
    finally:
        conn.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--exact", action="store_true",
                    help="walk dbstat for true page occupancy (needs "
                         "SQLITE_ENABLE_DBSTAT_VTAB; falls back to estimate)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"db not found: {db_path}", file=sys.stderr)
        return 2

    payload = audit(db_path, args.exact)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"{payload['db']}  mode={payload['mode']}  "
          f"file={payload['file']['file_gb']} GB")
    if payload["exact_error"]:
        print(f"  dbstat unavailable: {payload['exact_error']}", file=sys.stderr)
    for layer, vals in payload["layers"].items():
        print(f"  {layer:20s} {vals['gb']:9.4f} GB  {vals['pct_of_file'] or 0:6.2f}%")
    print(f"  {'(unaccounted)':20s} {payload['unaccounted_gb']:9.4f} GB")
    if payload["unknown_objects"]:
        print(f"  UNKNOWN objects (unmapped): {payload['unknown_objects']}",
              file=sys.stderr)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
