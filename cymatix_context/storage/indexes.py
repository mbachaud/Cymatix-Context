"""Index population helpers used during upsert_doc.

Extracted from knowledge_store.py (approach C: standalone functions).
These run inside the upsert transaction on the write ``conn`` -- they
take a cursor so the caller controls commit boundaries.

Covers:
    - promoter_index rebuild for a single document
    - FTS5 sync for a single document
    - entity_graph index for a single document
    - path_key_index population
    - filename_index population
    - SPLADE sparse index population
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, List, Optional

from ..schemas import Gene

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# promoter_index
# ---------------------------------------------------------------------------

def rebuild_promoter_index(
    cur: sqlite3.Cursor,
    gene_id: str,
    gene: Gene,
) -> None:
    """Delete + re-insert promoter_index rows for *gene_id*."""
    cur.execute("DELETE FROM promoter_index WHERE gene_id = ?", (gene_id,))

    for d in gene.promoter.domains:
        cur.execute(
            "INSERT INTO promoter_index VALUES (?, 'domain', ?)",
            (gene_id, d.lower()),
        )
    for e in gene.promoter.entities:
        cur.execute(
            "INSERT INTO promoter_index VALUES (?, 'entity', ?)",
            (gene_id, e.lower()),
        )


# ---------------------------------------------------------------------------
# FTS5 sync
# ---------------------------------------------------------------------------

def fetch_fts_source_row(
    cur: sqlite3.Cursor, gene_id: str,
) -> Optional[tuple]:
    """Current ``(rowid, gene_id, content, complement)`` for *gene_id* from
    the external-content backing view, or None.

    Issue #338: on an external-content table the FTS index stores no text
    of its own, so a delete must re-supply the EXACT token stream that was
    indexed. Callers capture this row BEFORE mutating ``genes`` /
    ``promoter_index`` (both feed the view) and hand it to
    :func:`fts_delete_row` afterwards.
    """
    try:
        row = cur.execute(
            "SELECT rowid, gene_id, content, complement "
            "FROM genes_fts_source WHERE gene_id = ?",
            (gene_id,),
        ).fetchone()
    except Exception:
        log.warning(
            "FTS5 source-view read failed for gene %s", gene_id, exc_info=True,
        )
        return None
    return tuple(row) if row is not None else None


def fts_index_has_rowid(cur: sqlite3.Cursor, rowid: int) -> bool:
    """True if the external-content FTS *index* contains *rowid*.

    ``SELECT ... FROM genes_fts WHERE rowid = ?`` resolves through the
    backing view, not the index, so it cannot answer this. The
    ``genes_fts_docsize`` shadow (one row per indexed doc at the default
    columnsize=1) can. Issuing a 'delete' for a rowid the index never
    absorbed corrupts it, so when the shadow is unreadable we err on the
    side of assuming membership (the common case by far).
    """
    try:
        return cur.execute(
            "SELECT 1 FROM genes_fts_docsize WHERE id = ?", (rowid,)
        ).fetchone() is not None
    except Exception:
        log.warning(
            "genes_fts_docsize probe failed — assuming rowid %d is indexed",
            rowid, exc_info=True,
        )
        return True


def fts_delete_row(cur: sqlite3.Cursor, prior_row: tuple) -> None:
    """Remove one row from the external-content FTS index.

    A plain ``DELETE FROM genes_fts`` is NOT legal on an external-content
    table — the index must be handed the prior row's indexed values via
    the special 'delete' insert so it can locate and remove the postings.
    *prior_row* is the ``(rowid, gene_id, content, complement)`` tuple
    captured by :func:`fetch_fts_source_row` before the mutation.
    """
    if not fts_index_has_rowid(cur, prior_row[0]):
        # Drift (a soft-failed insert whose gene row committed): nothing
        # to remove, and a bogus 'delete' would corrupt the index.
        return
    cur.execute(
        "INSERT INTO genes_fts(genes_fts, rowid, gene_id, content, complement) "
        "VALUES ('delete', ?, ?, ?, ?)",
        prior_row,
    )


def fts_insert_from_source(cur: sqlite3.Cursor, gene_id: str) -> None:
    """Index *gene_id*'s CURRENT view row into the external-content index.

    Values are read from the backing view (not rebuilt in Python) so the
    indexed token stream is exactly what a later delete-time view read
    will reproduce.
    """
    row = fetch_fts_source_row(cur, gene_id)
    if row is None:
        return
    cur.execute(
        "INSERT INTO genes_fts(rowid, gene_id, content, complement) "
        "VALUES (?, ?, ?, ?)",
        row,
    )


def sync_fts5(
    cur: sqlite3.Cursor,
    gene_id: str,
    gene: Gene,
    fts_available: bool,
    delete_first: bool = True,
    fts_external: bool = False,
    prior_fts_row: Optional[tuple] = None,
) -> None:
    """Sync one document into the FTS5 index.  No-op if FTS5 is unavailable.

    Legacy contentful form: ``genes_fts`` is a plain FTS5 virtual table —
    FTS5 has no UNIQUE constraints, so ``INSERT OR REPLACE`` silently
    *appends* a second row on re-upsert: stale terms stay searchable and
    BM25 doc counts inflate (bug bash 2026-07-18). Delete any prior row(s)
    for this gene_id first; both statements run inside the caller's upsert
    transaction so the swap stays atomic. ``delete_first=False`` lets the
    caller skip the delete (an unindexed full scan on FTS5) when it knows
    the gene is new — the bulk-ingest fast path.

    External-content form (issue #338, ``fts_external=True``): the index
    stores no text, so the delete must re-supply the PRIOR row's indexed
    values — the caller captures ``prior_fts_row`` via
    :func:`fetch_fts_source_row` BEFORE writing ``genes`` /
    ``promoter_index`` (an INSERT OR REPLACE changes the genes rowid and
    the new tag/content text, so a post-write view read cannot reproduce
    what was indexed). The new row is then read back from the view so the
    indexed stream matches future delete-time reads. ``delete_first``
    keeps the same fast-path meaning: False skips the delete for known-new
    genes.
    """
    if not fts_available:
        return
    try:
        if fts_external:
            if delete_first and prior_fts_row is not None:
                fts_delete_row(cur, prior_fts_row)
            fts_insert_from_source(cur, gene_id)
            return
        tag_text = " ".join(
            [d.lower() for d in gene.promoter.domains]
            + [e.lower() for e in gene.promoter.entities]
        )
        fts_content = f"{gene.source_id or ''} {tag_text} {gene.content}"
        if delete_first:
            cur.execute(
                "DELETE FROM genes_fts WHERE gene_id = ?", (gene_id,)
            )
        cur.execute(
            "INSERT INTO genes_fts(gene_id, content, complement) "
            "VALUES (?, ?, ?)",
            (gene_id, fts_content, gene.complement or ""),
        )
    except Exception:
        log.warning(
            "FTS5 sync failed for gene %s", gene_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Entity graph index
# ---------------------------------------------------------------------------

def sync_entity_graph(
    cur: sqlite3.Cursor,
    gene_id: str,
    gene: Gene,
    entity_graph_enabled: bool,
    autolink_enabled: bool = True,
    hub_cutoff: int = 0,
) -> None:
    """Index entities and auto-link by shared entities.

    ``autolink_enabled=False`` (2026-08-30 ingest-decay receipt) writes the
    entity rows but skips COVER-edge formation — the per-insert
    ``auto_link_by_entity`` GROUP BY sweeps every posting row of the gene's
    entities, O(hub size) per insert, and hub postings grow with the corpus.

    ``hub_cutoff`` — posting-count bound for the auto-link probe set
    ([ingestion] entity_autolink_hub_cutoff); 0 = legacy behavior. See
    :func:`co_activation.auto_link_by_entity`. Counts are taken after
    this gene's own rows land, so they include the self-posting.

    Composition (off > cutoff > legacy): ``autolink_enabled=False`` skips
    linking entirely; otherwise ``hub_cutoff`` bounds the probe set.
    """
    if not entity_graph_enabled or not gene.promoter.entities:
        return

    from .co_activation import auto_link_by_entity

    cur.execute("DELETE FROM entity_graph WHERE gene_id = ?", (gene_id,))
    for ent in gene.promoter.entities[:15]:
        cur.execute(
            "INSERT OR IGNORE INTO entity_graph (entity, gene_id) VALUES (?, ?)",
            (ent.lower(), gene_id),
        )
    if autolink_enabled:
        auto_link_by_entity(gene_id, gene.promoter.entities, cur, hub_cutoff=hub_cutoff)


# ---------------------------------------------------------------------------
# path_key_index
# ---------------------------------------------------------------------------

# Tier-0 scoring constants (issue #165: canonical home — the retrieval
# scorer in knowledge_store.py and the compactor below MUST agree on the
# noise cutoff, otherwise compaction could prune rows the scorer still
# credits).
#
#   per-pair bonus = PKI_BASE / max(pair_cardinality, PKI_FLOOR)
#   pairs with cardinality > PKI_NOISE_CUTOFF are hard-skipped (no score)
PKI_BASE = 10.0
PKI_FLOOR = 2.0
PKI_NOISE_CUTOFF = 200


def compact_path_key_index(
    conn: sqlite3.Connection,
    noise_cutoff: int = PKI_NOISE_CUTOFF,
    dry_run: bool = False,
    drop: bool = False,
    pki_enabled: bool = True,
) -> Dict:
    """Issue #165 Option-B compaction for ``path_key_index``.

    Three probe-backed, score-invariant moves (see
    ``165_path_key_index_consensus.md`` / issue #165 audit):

    1. **Drop ``idx_pki_lookup``** — EXPLAIN QUERY PLAN shows the live
       Tier-0 lookup runs as a COVERING scan of the (path_token, kv_key,
       gene_id) primary key; the (path_token, kv_key) index is a strict
       prefix of it and is never chosen (~7% of corpus dead weight on the
       v2 Onyx audit).
    2. **Prune dead pairs** — rows whose (path_token, kv_key) pair has
       cardinality > ``noise_cutoff`` are hard-skipped by the scorer
       (``PKI_NOISE_CUTOFF``), so they can never contribute score
       (38% of rows on the v2 Onyx audit). 40/40-query ablation showed
       byte-identical Tier-0 bonuses after pruning.
    3. **Rebuild WITHOUT ROWID** — the legacy rowid table + 3-column PK
       autoindex store every row twice; a WITHOUT ROWID table stores it
       once, in PK order (same covering-lookup plan).

    The rebuild runs in a single transaction (CREATE new / anti-join copy
    / DROP old / RENAME), so a crash rolls back to the untouched legacy
    table. Freed pages go to the SQLite freelist — run ``VACUUM``
    (``/admin/vacuum``) afterwards to shrink the file.

    Pruned rows regrow for newly-upserted genes (cardinality is only
    knowable globally), but stay inert at query time thanks to the same
    cutoff — re-run compaction periodically on bulk-ingest corpora.

    **Drop mode (issue #370).** With ``drop=True`` the table (and its
    indexes) is dropped entirely instead of compacted — the reclaim path
    for stores that run with ``[retrieval] pki_enabled = false``, where
    the Tier-0 scorer never reads the index and the bytes are pure dead
    weight (8.56 GB / 18.5% of the 829k blob bed). Scorer-neutrality is
    structural: a pki-off store issues no SQL against this table.

    Guards and caveats:

    - The caller MUST pass the store's effective ``pki_enabled`` flag.
      ``drop=True`` with ``pki_enabled=True`` raises ``ValueError`` —
      dropping a table the scorer still reads is never allowed.
    - The drop only moves pages to the SQLite freelist; run ``VACUUM``
      (``/admin/vacuum``) afterwards or no bytes are reclaimed on disk.
    - The DDL bootstrap (``storage.ddl._create_path_key_index``)
      recreates an EMPTY table on the next store open, and upserts
      repopulate it for newly-ingested genes — the drop reclaims the
      historical rows, it is not a write gate. Re-run between ingest
      waves, or rebuild the full index with
      ``scripts/backfill_path_key_index.py --apply`` if ``pki_enabled``
      is ever flipped back on.

    Returns a report dict; with ``dry_run=True`` nothing is modified.
    """
    cur = conn.cursor()

    if drop and pki_enabled:
        raise ValueError(
            "refusing to drop path_key_index: this store runs with "
            "[retrieval] pki_enabled = true, so the Tier-0 scorer still "
            "reads the table. Set pki_enabled = false in cymatix.toml "
            "(and restart) before dropping, or run the prune-only "
            "compaction (drop=false) instead. (issue #370)"
        )

    def _exists(kind: str, name: str) -> bool:
        return cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
            (kind, name),
        ).fetchone() is not None

    if drop:
        report = {"dry_run": dry_run, "drop": True}
        if not _exists("table", "path_key_index"):
            report["skipped"] = "no path_key_index table"
            return report
        rows_before = cur.execute(
            "SELECT COUNT(*) FROM path_key_index").fetchone()[0]
        report["rows_before"] = rows_before
        if dry_run:
            report["would_drop"] = True
            return report
        try:
            cur.execute("BEGIN IMMEDIATE")
            # DROP TABLE takes idx_pki_gene / idx_pki_lookup /
            # the PK autoindex down with it.
            cur.execute("DROP TABLE path_key_index")
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        report["dropped"] = True
        report["hint"] = (
            "pages moved to the SQLite freelist — run VACUUM "
            "(/admin/vacuum) to reclaim them on disk; rebuild with "
            "scripts/backfill_path_key_index.py --apply if "
            "[retrieval] pki_enabled is re-enabled"
        )
        log.info(
            "path_key_index dropped (#370 reclaim, pki-off store): "
            "%d rows released to the freelist — VACUUM to shrink the file",
            rows_before,
        )
        return report

    report: Dict = {
        "dry_run": dry_run,
        "noise_cutoff": noise_cutoff,
        "had_lookup_index": _exists("index", "idx_pki_lookup"),
        # A legacy rowid table materializes its 3-col PK as a
        # sqlite_autoindex; the WITHOUT ROWID rebuild has none.
        "was_rowid_table": _exists(
            "index", "sqlite_autoindex_path_key_index_1"
        ),
    }
    if not _exists("table", "path_key_index"):
        report["skipped"] = "no path_key_index table"
        return report

    rows_before = cur.execute(
        "SELECT COUNT(*) FROM path_key_index").fetchone()[0]
    dead = cur.execute(
        "SELECT COALESCE(SUM(c), 0), COUNT(*) FROM ("
        "  SELECT COUNT(*) c FROM path_key_index"
        "  GROUP BY path_token, kv_key HAVING COUNT(*) > ?)",
        (noise_cutoff,),
    ).fetchone()
    report["rows_before"] = rows_before
    report["dead_rows"] = dead[0]
    report["dead_pairs"] = dead[1]

    needs_rebuild = (
        report["was_rowid_table"] or report["dead_rows"] > 0
    )
    report["needs_rebuild"] = needs_rebuild
    if dry_run:
        return report

    try:
        cur.execute("BEGIN IMMEDIATE")
        if report["had_lookup_index"]:
            cur.execute("DROP INDEX idx_pki_lookup")
        if needs_rebuild:
            cur.execute("""
            CREATE TABLE path_key_index_compact (
                path_token TEXT NOT NULL,
                kv_key     TEXT NOT NULL,
                gene_id    TEXT NOT NULL,
                PRIMARY KEY (path_token, kv_key, gene_id)
            ) WITHOUT ROWID
            """)
            cur.execute(
                "CREATE TEMP TABLE _dead_pairs AS "
                "SELECT path_token, kv_key FROM path_key_index "
                "GROUP BY path_token, kv_key HAVING COUNT(*) > ?",
                (noise_cutoff,),
            )
            cur.execute(
                "CREATE INDEX temp._dead_pairs_idx "
                "ON _dead_pairs(path_token, kv_key)"
            )
            cur.execute(
                "INSERT INTO path_key_index_compact "
                "SELECT path_token, kv_key, gene_id FROM path_key_index o "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM _dead_pairs d "
                "  WHERE d.path_token = o.path_token "
                "  AND d.kv_key = o.kv_key)"
            )
            cur.execute("DROP TABLE path_key_index")
            cur.execute(
                "ALTER TABLE path_key_index_compact "
                "RENAME TO path_key_index"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_pki_gene "
                "ON path_key_index(gene_id)"
            )
            cur.execute("DROP TABLE _dead_pairs")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise

    report["rows_after"] = cur.execute(
        "SELECT COUNT(*) FROM path_key_index").fetchone()[0]
    report["rows_pruned"] = rows_before - report["rows_after"]
    report["hint"] = "run VACUUM (/admin/vacuum) to reclaim freed pages"
    log.info(
        "path_key_index compaction: %d -> %d rows (-%d dead, cutoff=%d), "
        "lookup_index_dropped=%s, without_rowid=%s",
        rows_before, report["rows_after"], report["rows_pruned"],
        noise_cutoff, report["had_lookup_index"], needs_rebuild,
    )
    return report


def sync_path_key_index(
    cur: sqlite3.Cursor,
    gene_id: str,
    gene: Gene,
) -> None:
    """Populate the compound (path_token, kv_key) -> gene_id index.

    Issue #370: on a pki-off store the operator may have dropped the
    table entirely (``compact_path_key_index(drop=True)``). A missing
    table degrades to a no-op — same optional-table treatment as
    ``delete_gene`` — instead of aborting the whole upsert transaction.
    The DDL bootstrap recreates the (empty) table on the next store open.
    """
    # Import module-level helpers from the parent package.  These are
    # standalone functions that were always module-level in knowledge_store.py.
    from ..knowledge_store import path_tokens, _kv_keys_from_list

    try:
        cur.execute("DELETE FROM path_key_index WHERE gene_id = ?", (gene_id,))
        if gene.source_id and gene.key_values:
            p_tokens = path_tokens(gene.source_id)
            kv_keys = _kv_keys_from_list(gene.key_values)
            if p_tokens and kv_keys:
                for pt in p_tokens:
                    for kk in kv_keys:
                        cur.execute(
                            "INSERT OR IGNORE INTO path_key_index "
                            "(path_token, kv_key, gene_id) VALUES (?, ?, ?)",
                            (pt, kk, gene_id),
                        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            log.debug(
                "path_key_index sync skipped for gene %s "
                "(table dropped, #370): %s", gene_id, exc,
            )
            return
        raise


# ---------------------------------------------------------------------------
# filename_index
# ---------------------------------------------------------------------------

def sync_filename_index(
    cur: sqlite3.Cursor,
    gene_id: str,
    source_id: Optional[str],
) -> None:
    """Populate the filename-anchor reverse index."""
    cur.execute("DELETE FROM filename_index WHERE gene_id = ?", (gene_id,))
    try:
        from .. import filename_anchor as _fa
        _fa.index_gene(cur.connection, gene_id, source_id)
    except Exception:
        log.debug("filename_index upsert skipped for gene=%s", gene_id, exc_info=True)


# ---------------------------------------------------------------------------
# SPLADE sparse index
# ---------------------------------------------------------------------------

def resolve_splade_enabled(
    splade_enabled: bool,
    current_gene_count: int,
    auto_enable_below: int = 0,
    auto_disable_above: int = 0,
) -> bool:
    """Apply the size-aware SPLADE auto-toggle (issue #164).

    The static ``splade_enabled`` flag is overridden when the corpus
    crosses one of the two opt-in thresholds:

      - ``auto_enable_below > 0`` and ``current_gene_count <
        auto_enable_below``: force ON (sparse-corpus rescue arm). Used
        when the user wants SPLADE only on small corpora where lexical
        expansion is most likely to rescue a paraphrase mismatch.

      - ``auto_disable_above > 0`` and ``current_gene_count >
        auto_disable_above``: force OFF (enterprise-scale storage cliff
        arm). Used when SPLADE's disk + p95 + SQL fan-out cost dominates
        at the head of the corpus-size distribution and recall doesn't
        move (see #164 storage breakdown).

    When BOTH thresholds are ``0`` (the default opt-in floor) the static
    flag is returned unchanged -- byte-identical to pre-#164 behaviour.
    When the corpus is in the gray band (``auto_enable_below <=
    current_gene_count <= auto_disable_above``) the static
    ``splade_enabled`` value also wins; the toggle is only authoritative
    at the named ends of the size curve.

    Boundary semantics (deliberately strict so the toggle is a hard
    binary at the bound, not a fuzzy band):
      - ``current_gene_count == auto_enable_below`` -> NOT auto-enabled
        (caller is at-or-above the rescue ceiling)
      - ``current_gene_count == auto_disable_above`` -> NOT auto-disabled
        (caller is at-or-below the disable floor)

    Caller contract for ``current_gene_count``:
        ``upsert_doc`` queries ``COUNT(*) FROM genes`` AFTER the row's
        INSERT (the INSERT runs before the index-population stage), so
        ``current_gene_count`` includes the gene being upserted. With
        ``auto_disable_above_genes=N`` the disable arm fires on the
        ``(N+1)``-th gene to be upserted.

    Return value:
        The effective ``splade_enabled`` for the current upsert. The
        caller (typically ``sync_splade_index``) writes nothing when this
        is ``False``.
    """
    if auto_disable_above > 0 and current_gene_count > auto_disable_above:
        return False
    if auto_enable_below > 0 and current_gene_count < auto_enable_below:
        return True
    return bool(splade_enabled)


def sync_splade_index(
    cur: sqlite3.Cursor,
    gene_id: str,
    content: str,
    splade_enabled: bool,
    splade_sparse: Optional[Dict[str, float]] = None,
    content_cap: int = 1000,
    model_name: str = "naver/splade-cocondenser-ensembledistil",
) -> None:
    """Populate the SPLADE sparse-term index.  No-op when disabled.

    When ``splade_sparse`` is None (default), encode ``content[:1000]``
    inline via :mod:`cymatix_context.backends.splade_backend`. When provided,
    use the supplied sparse dict as-is and skip the inline encode -- used
    by the parallel ingest paths (issue #92) so SPLADE can be batched
    outside the per-document upsert.
    """
    if not splade_enabled:
        return
    try:
        if splade_sparse is None:
            from ..backends import splade_backend
            splade_sparse = splade_backend.encode(
                content[:content_cap], model_name=model_name)
        cur.execute("DELETE FROM splade_terms WHERE gene_id = ?", (gene_id,))
        if splade_sparse:
            cur.executemany(
                "INSERT INTO splade_terms (gene_id, term, weight) VALUES (?, ?, ?)",
                [(gene_id, term, weight) for term, weight in splade_sparse.items()],
            )
    except Exception:
        log.debug("SPLADE indexing failed for gene %s", gene_id, exc_info=True)
