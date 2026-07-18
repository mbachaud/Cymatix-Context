"""Write-path integrity tests for the knowledge store (2026-07-18 bug bash).

Covers three storage-layer defects flagged by external review:

  BUG-1  upsert_doc shares one unlocked write connection across threads —
         a concurrent commit could publish another request's half-written
         transaction, and a failed upsert left its partial statements in
         the open transaction for the next commit to sweep in.
  BUG-2  genes_fts is a plain FTS5 table (no UNIQUE constraint), so
         "INSERT OR REPLACE" silently APPENDED a second row per re-upsert:
         stale terms stayed searchable and BM25 doc counts inflated.
  BUG-3  gene IDs are content-addressed (sha256(content)[:16]) — identical
         content from two sources collapses to one row and the later
         ingest silently overwrites the earlier source's provenance.
         Dedup itself is by design; the overwrite must at least be loud.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from helix_context.genome import Genome
from tests.conftest import make_gene


# ---------------------------------------------------------------------------
# BUG-2: FTS5 upsert must not append duplicate rows
# ---------------------------------------------------------------------------

class TestFtsUpsertNoDuplicates:
    def test_reupsert_leaves_single_fts_row(self):
        g = Genome(path=":memory:", synonym_map={})
        try:
            gene = make_gene("alpha bravo payload", gene_id="fixedid000000001")
            g.upsert_gene(gene)
            # Same gene_id, updated content — the FTS row must be replaced,
            # not appended.
            updated = make_gene(
                "alpha charlie payload", gene_id="fixedid000000001"
            )
            g.upsert_gene(updated)
            n = g.conn.execute(
                "SELECT COUNT(*) FROM genes_fts WHERE gene_id = ?",
                ("fixedid000000001",),
            ).fetchone()[0]
            assert n == 1, f"expected 1 FTS row after re-upsert, found {n}"
        finally:
            g.close()

    def test_stale_terms_no_longer_searchable(self):
        g = Genome(path=":memory:", synonym_map={})
        try:
            gene = make_gene("obsoleteterm bravo payload", gene_id="fixedid000000002")
            g.upsert_gene(gene)
            updated = make_gene(
                "freshterm charlie payload", gene_id="fixedid000000002"
            )
            g.upsert_gene(updated)
            stale = g.conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'obsoleteterm'"
            ).fetchall()
            assert stale == [], (
                "deleted term still matches in FTS5 after re-upsert "
                f"(rows: {[r[0] for r in stale]})"
            )
            fresh = g.conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'freshterm'"
            ).fetchall()
            assert [r[0] for r in fresh] == ["fixedid000000002"]
        finally:
            g.close()


# ---------------------------------------------------------------------------
# BUG-1: upsert transaction discipline under failure and concurrency
# ---------------------------------------------------------------------------

class TestUpsertTransactionDiscipline:
    def test_failed_upsert_is_rolled_back(self, monkeypatch):
        """A mid-upsert failure must not leave the partial row for the next
        commit to sweep in."""
        import helix_context.storage.indexes as idx

        g = Genome(path=":memory:", synonym_map={})
        try:
            doomed = make_gene("doomed payload", domains=["doom"])

            def boom(cur, gene_id, gene):
                raise RuntimeError("index build exploded")

            with monkeypatch.context() as m:
                m.setattr(idx, "rebuild_promoter_index", boom)
                with pytest.raises(RuntimeError):
                    g.upsert_gene(doomed)

            # A subsequent healthy upsert commits. The doomed gene's genes
            # row was executed before the failure — without a rollback it
            # rides along with this commit.
            g.upsert_gene(make_gene("healthy payload", domains=["ok"]))
            row = g.conn.execute(
                "SELECT 1 FROM genes WHERE gene_id = ?", (doomed.gene_id,)
            ).fetchone()
            assert row is None, (
                "partial row from a FAILED upsert was committed by the next "
                "successful upsert (missing rollback)"
            )
        finally:
            g.close()

    def test_concurrent_upserts_do_not_publish_partial_rows(
        self, tmp_path: Path, monkeypatch
    ):
        """Thread B's commit must not publish thread A's half-written gene
        (genes row present, index rows absent)."""
        import helix_context.storage.indexes as idx

        db = str(tmp_path / "genome.db")
        g = Genome(path=db, synonym_map={})
        gene_a = make_gene(
            "gene alpha payload", domains=["alpha"], entities=["AlphaEnt"]
        )
        gene_b = make_gene(
            "gene bravo payload", domains=["bravo"], entities=["BravoEnt"]
        )

        a_inside = threading.Event()
        release_a = threading.Event()
        real_rebuild = idx.rebuild_promoter_index

        def hooked(cur, gene_id, gene):
            # Stall thread A between its genes INSERT and its index sync so
            # thread B gets a window to run a full upsert + commit.
            if gene_id == gene_a.gene_id:
                a_inside.set()
                release_a.wait(timeout=10)
            real_rebuild(cur, gene_id, gene)

        monkeypatch.setattr(idx, "rebuild_promoter_index", hooked)

        errors: list[Exception] = []

        def run(gene):
            try:
                g.upsert_gene(gene)
            except Exception as exc:  # pragma: no cover - diagnostic
                errors.append(exc)

        ta = threading.Thread(target=run, args=(gene_a,))
        ta.start()
        try:
            assert a_inside.wait(timeout=10), "thread A never reached index sync"
            tb = threading.Thread(target=run, args=(gene_b,))
            tb.start()
            # Unserialized, B commits within this window — sweeping in A's
            # partial genes row. Serialized, B just blocks until A finishes.
            tb.join(timeout=2.0)

            reader = sqlite3.connect(db)
            try:
                committed_a = reader.execute(
                    "SELECT 1 FROM genes WHERE gene_id = ?", (gene_a.gene_id,)
                ).fetchone()
                if committed_a is not None:
                    n_tags = reader.execute(
                        "SELECT COUNT(*) FROM promoter_index WHERE gene_id = ?",
                        (gene_a.gene_id,),
                    ).fetchone()[0]
                    assert n_tags > 0, (
                        "another thread's commit published gene A's content "
                        "without its index rows (interleaved transaction)"
                    )
            finally:
                reader.close()
        finally:
            release_a.set()
            ta.join(timeout=10)
        tb.join(timeout=10)
        assert not errors, f"upsert threads raised: {errors}"

        # Both genes fully indexed once the dust settles.
        for gid in (gene_a.gene_id, gene_b.gene_id):
            n_tags = g.conn.execute(
                "SELECT COUNT(*) FROM promoter_index WHERE gene_id = ?", (gid,)
            ).fetchone()[0]
            assert n_tags > 0
        g.close()


# ---------------------------------------------------------------------------
# BUG-3: content-hash collision across sources must not be silent
# ---------------------------------------------------------------------------

class TestContentCollisionProvenance:
    def test_cross_source_overwrite_emits_warning(self, caplog):
        g = Genome(path=":memory:", synonym_map={})
        try:
            content = "identical payload shared by two files"
            first = make_gene(content)
            first.source_id = "docs/a.md"
            second = make_gene(content)
            second.source_id = "docs/b.md"
            assert first.gene_id == second.gene_id  # content-addressed by design

            g.upsert_gene(first)
            with caplog.at_level(
                logging.WARNING, logger="helix_context.knowledge_store"
            ):
                g.upsert_gene(second)
            assert any(
                "provenance" in rec.getMessage() for rec in caplog.records
            ), "cross-source provenance overwrite happened silently"

            # Documented current behavior: last writer wins (content dedup
            # is by design; changing the winner is a design decision).
            row = g.conn.execute(
                "SELECT source_id FROM genes WHERE gene_id = ?",
                (first.gene_id,),
            ).fetchone()
            assert row["source_id"] == "docs/b.md"
        finally:
            g.close()

    def test_same_source_reupsert_stays_quiet(self, caplog):
        g = Genome(path=":memory:", synonym_map={})
        try:
            content = "stable payload from one file"
            gene = make_gene(content)
            gene.source_id = "docs/a.md"
            g.upsert_gene(gene)
            with caplog.at_level(
                logging.WARNING, logger="helix_context.knowledge_store"
            ):
                g.upsert_gene(gene)
            assert not any(
                "provenance" in rec.getMessage() for rec in caplog.records
            ), "same-source re-upsert must not warn"
        finally:
            g.close()
