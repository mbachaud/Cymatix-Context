"""FTS5 external-content rebuild tests (issue #338).

The contentful ``genes_fts`` table stored its own full copy of every
document (the ``genes_fts_content`` shadow — 8.67 GB / 18.5% of the 829K
blob bed). The external-content form indexes the SAME composite text
(source_id + promoter tags + content, complement) through the
``genes_fts_source`` view, so tokenizer, BM25 stats, and column reads are
identical — only the redundant shadow copy is gone.

Covers:
  1. New stores are created in the external-content form.
  2. External-content write discipline: re-upsert replaces (no stale
     terms, no duplicate rows), delete_gene removes index entries, and
     the FTS integrity check passes after every mutation.
  3. Legacy contentful stores still open, query, and upsert untouched.
  4. scripts/migrate_fts5_external_content.py: byte-identical
     (gene_id, rank) query results pre/post, idempotence, --dry-run.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cymatix_context.genome import Genome
from cymatix_context.storage.ddl import (
    FTS_SOURCE_VIEW,
    fts5_is_external_content,
)
from tests.conftest import make_gene


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DOCS = [
    ("retrieval fusion ranks candidates by reciprocal rank", ["retrieval"], ["rrf"]),
    ("the splice stage compresses candidate fragments", ["pipeline"], ["splice"]),
    ("sqlite fts5 index stores tokenized postings", ["storage"], ["fts5"]),
    ("dense embeddings recall paraphrased needles", ["retrieval"], ["bgem3"]),
    ("the freshness gate demotes stale documents", ["pipeline"], ["freshness"]),
    ("promoter tags map queries onto document domains", ["tags"], []),
    ("reciprocal rank fusion beat the additive accumulator", ["retrieval"], ["rrf"]),
    ("wal checkpoint truncates the write ahead log", ["storage"], ["wal"]),
]

QUERIES = [
    '"retrieval"',
    '"rrf" OR "fusion"',
    '"splice" OR "fragments"',
    '"sqlite" OR "index" OR "postings"',
    '"stale" OR "freshness" OR "gate"',
    '"reciprocal" OR "rank"',
]


def _build_store(path: Path) -> list[str]:
    """Create a store at *path* with the DOCS corpus; return gene_ids."""
    g = Genome(path=str(path), synonym_map={})
    gene_ids = []
    try:
        for content, domains, entities in DOCS:
            gene = make_gene(content, domains=domains, entities=entities)
            g.upsert_gene(gene)
            gene_ids.append(gene.gene_id)
    finally:
        g.close()
    return gene_ids


def _fts_create_sql(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'genes_fts'"
    ).fetchone()
    return row[0] if row else ""


def _run_queries(conn: sqlite3.Connection) -> list[tuple]:
    out = []
    for q in QUERIES:
        rows = conn.execute(
            "SELECT gene_id, rank FROM genes_fts WHERE genes_fts MATCH ? "
            "ORDER BY rank, gene_id",
            (q,),
        ).fetchall()
        out.append((q, [(r[0], r[1]) for r in rows]))
    return out


def _integrity_ok(conn: sqlite3.Connection) -> bool:
    """FTS5 integrity check; rank=1 also verifies index-vs-content."""
    try:
        conn.execute(
            "INSERT INTO genes_fts(genes_fts, rank) VALUES ('integrity-check', 1)"
        )
    except sqlite3.OperationalError:
        # Older SQLite without the two-arg form.
        conn.execute("INSERT INTO genes_fts(genes_fts) VALUES ('integrity-check')")
    return True


def _downgrade_to_legacy(path: Path) -> None:
    """Rebuild *path*'s genes_fts in the pre-#338 plain contentful form."""
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS genes_fts_vocab")
        cur.execute("DROP TABLE IF EXISTS genes_fts")
        cur.execute(f"DROP VIEW IF EXISTS {FTS_SOURCE_VIEW}")
        cur.execute(
            "CREATE VIRTUAL TABLE genes_fts USING fts5(gene_id, content, complement)"
        )
        cur.execute(
            "INSERT INTO genes_fts(gene_id, content, complement) "
            "SELECT g.gene_id, "
            "  COALESCE(g.source_id,'') || ' ' || "
            "  COALESCE((SELECT GROUP_CONCAT(pi.tag_value, ' ') "
            "    FROM promoter_index pi WHERE pi.gene_id = g.gene_id), '') "
            "  || ' ' || g.content, "
            "  COALESCE(g.complement, '') "
            "FROM genes g"
        )
        cur.execute(
            "CREATE VIRTUAL TABLE genes_fts_vocab "
            "USING fts5vocab(genes_fts, instance)"
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. New-store DDL form
# ---------------------------------------------------------------------------

class TestNewStoreForm:
    def test_new_store_is_external_content(self, tmp_path):
        db = tmp_path / "genome.db"
        _build_store(db)
        conn = sqlite3.connect(str(db))
        try:
            sql = _fts_create_sql(conn)
            assert f"content='{FTS_SOURCE_VIEW}'" in sql.replace('"', "'"), sql
            assert fts5_is_external_content(conn.cursor())
            view = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?",
                (FTS_SOURCE_VIEW,),
            ).fetchone()
            assert view is not None, "backing view missing"
            vocab = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='genes_fts_vocab'"
            ).fetchone()
            assert vocab is not None, "fts5vocab shadow missing"
        finally:
            conn.close()

    def test_new_store_has_no_contentful_shadow(self, tmp_path):
        db = tmp_path / "genome.db"
        _build_store(db)
        conn = sqlite3.connect(str(db))
        try:
            shadow = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='genes_fts_content'"
            ).fetchone()
            assert shadow is None, "contentful shadow present on a new store"
        finally:
            conn.close()

    def test_new_store_match_and_integrity(self, tmp_path):
        db = tmp_path / "genome.db"
        gene_ids = _build_store(db)
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'splice'"
            ).fetchall()
            assert [r[0] for r in rows] == [gene_ids[1]]
            assert _integrity_ok(conn)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 2. External-content write discipline
# ---------------------------------------------------------------------------

class TestExternalWriteDiscipline:
    def test_reupsert_replaces_and_integrity_holds(self, tmp_path):
        db = tmp_path / "genome.db"
        g = Genome(path=str(db), synonym_map={})
        try:
            gene = make_gene("obsoleteterm bravo payload", gene_id="extid0000000001")
            g.upsert_gene(gene)
            updated = make_gene("freshterm charlie payload", gene_id="extid0000000001")
            g.upsert_gene(updated)
            stale = g.conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'obsoleteterm'"
            ).fetchall()
            assert stale == [], "stale terms still searchable after re-upsert"
            fresh = g.conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'freshterm'"
            ).fetchall()
            assert [r[0] for r in fresh] == ["extid0000000001"]
            assert _integrity_ok(g.conn)
        finally:
            g.close()

    def test_delete_gene_removes_index_entries(self, tmp_path):
        db = tmp_path / "genome.db"
        g = Genome(path=str(db), synonym_map={})
        try:
            gene = make_gene("doomedterm delta payload", gene_id="extid0000000002")
            g.upsert_gene(gene)
            assert g.delete_gene("extid0000000002")
            rows = g.conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'doomedterm'"
            ).fetchall()
            assert rows == [], "deleted gene still matches in FTS5"
            assert _integrity_ok(g.conn)
        finally:
            g.close()

    def test_global_idf_rowid_mapping_still_resolves(self, tmp_path):
        """The #182 rescore maps gene_id -> rowid through genes_fts; on the
        external form that full scan resolves through the backing view."""
        db = tmp_path / "genome.db"
        gene_ids = _build_store(db)
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT rowid, gene_id FROM genes_fts WHERE gene_id IN (?, ?)",
                (gene_ids[0], gene_ids[2]),
            ).fetchall()
            assert {r[1] for r in rows} == {gene_ids[0], gene_ids[2]}
            # rowids must be the genes rowids (the vocab 'doc' values).
            for rid, gid in rows:
                real = conn.execute(
                    "SELECT rowid FROM genes WHERE gene_id = ?", (gid,)
                ).fetchone()[0]
                assert rid == real
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 3. Legacy contentful stores keep working untouched
# ---------------------------------------------------------------------------

class TestLegacyCompat:
    def test_legacy_store_opens_and_queries(self, tmp_path):
        db = tmp_path / "genome.db"
        gene_ids = _build_store(db)
        _downgrade_to_legacy(db)

        g = Genome(path=str(db), synonym_map={})
        try:
            # Form must be preserved — no forced migration at open.
            sql = _fts_create_sql(g.conn)
            assert "content=" not in sql.replace('"', "'"), (
                "legacy store was force-migrated at open"
            )
            rows = g.conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'splice'"
            ).fetchall()
            assert [r[0] for r in rows] == [gene_ids[1]]
        finally:
            g.close()

    def test_legacy_store_upsert_keeps_legacy_discipline(self, tmp_path):
        db = tmp_path / "genome.db"
        _build_store(db)
        _downgrade_to_legacy(db)

        g = Genome(path=str(db), synonym_map={})
        try:
            gene = make_gene("legacyterm alpha payload", gene_id="legid0000000001")
            g.upsert_gene(gene)
            updated = make_gene("legacyfresh beta payload", gene_id="legid0000000001")
            g.upsert_gene(updated)
            n = g.conn.execute(
                "SELECT COUNT(*) FROM genes_fts WHERE gene_id = ?",
                ("legid0000000001",),
            ).fetchone()[0]
            assert n == 1
            stale = g.conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'legacyterm'"
            ).fetchall()
            assert stale == []
        finally:
            g.close()


# ---------------------------------------------------------------------------
# 4. Migration script
# ---------------------------------------------------------------------------

class TestMigration:
    def _migrate(self, db: Path, **kwargs):
        from scripts.migrate_fts5_external_content import migrate
        return migrate(db, **kwargs)

    def test_migration_query_parity_byte_identical(self, tmp_path):
        db = tmp_path / "genome.db"
        _build_store(db)
        _downgrade_to_legacy(db)

        conn = sqlite3.connect(str(db))
        before = _run_queries(conn)
        conn.close()

        report = self._migrate(db)
        assert report["status"] == "migrated"

        conn = sqlite3.connect(str(db))
        try:
            after = _run_queries(conn)
            assert before == after, (
                "post-migration (gene_id, rank) results diverged from the "
                "legacy contentful index"
            )
            assert fts5_is_external_content(conn.cursor())
            shadow = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='genes_fts_content'"
            ).fetchone()
            assert shadow is None, "contentful shadow survived migration"
            vocab = conn.execute(
                "SELECT COUNT(*) FROM genes_fts_vocab"
            ).fetchone()[0]
            assert vocab > 0, "genes_fts_vocab not functional after migration"
            assert _integrity_ok(conn)
        finally:
            conn.close()

    def test_migration_dry_run_touches_nothing(self, tmp_path):
        db = tmp_path / "genome.db"
        _build_store(db)
        _downgrade_to_legacy(db)

        report = self._migrate(db, dry_run=True)
        assert report["status"] == "dry_run"
        assert report["estimated_reclaim_bytes"] > 0

        conn = sqlite3.connect(str(db))
        try:
            assert not fts5_is_external_content(conn.cursor())
        finally:
            conn.close()

    def test_migration_is_idempotent(self, tmp_path):
        db = tmp_path / "genome.db"
        _build_store(db)
        _downgrade_to_legacy(db)

        first = self._migrate(db)
        assert first["status"] == "migrated"
        second = self._migrate(db)
        assert second["status"] == "already_migrated"

    def test_migrated_store_supports_update_flows(self, tmp_path):
        db = tmp_path / "genome.db"
        _build_store(db)
        _downgrade_to_legacy(db)
        self._migrate(db)

        g = Genome(path=str(db), synonym_map={})
        try:
            gene = make_gene("migrterm alpha payload", gene_id="migid0000000001")
            g.upsert_gene(gene)
            updated = make_gene("migrfresh beta payload", gene_id="migid0000000001")
            g.upsert_gene(updated)
            stale = g.conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'migrterm'"
            ).fetchall()
            assert stale == []
            assert g.delete_gene("migid0000000001")
            assert _integrity_ok(g.conn)
        finally:
            g.close()

    def test_missing_db_is_a_clean_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            self._migrate(tmp_path / "nope.db")


# ---------------------------------------------------------------------------
# 4b. Stub-retention guard (cc-exchange 0017-joe §6, 2026-08-07)
# ---------------------------------------------------------------------------

class TestStubGuard:
    """The contentful shadow accidentally retains pre-compression original
    text (upsert indexes full content BEFORE compress_to_euchromatin stubs
    the genes row, and compression never touches FTS). Rebuilding from
    current genes.content destroys the only copy — the migration must
    refuse on stubbed stores unless the loss is explicitly accepted."""

    STUB_TEXT = "[COMPRESSED:euchromatin] source=src/original.py"
    # compress_to_euchromatin keeps `complement` (the summary = content[:50]
    # via make_gene), so a term is FTS-recoverable-only when it sits past
    # that window in the raw content — like this needle.
    STUB_DOC = (
        "padding words push the recoverable needle well past the summary "
        "window: uniqstubneedle"
    )

    def _migrate(self, db: Path, **kwargs):
        from scripts.migrate_fts5_external_content import migrate
        return migrate(db, **kwargs)

    def _stubbed_legacy_store(self, tmp_path: Path) -> tuple:
        """Legacy store whose FTS still holds a stubbed gene's original
        text — the accidental-retention condition from the analysis."""
        db = tmp_path / "genome.db"
        _build_store(db)
        g = Genome(path=str(db), synonym_map={})
        try:
            stub_gene = make_gene(self.STUB_DOC, gene_id="stubgene00000001")
            g.upsert_gene(stub_gene)
        finally:
            g.close()
        _downgrade_to_legacy(db)  # legacy FTS built from FULL originals
        conn = sqlite3.connect(str(db))
        try:
            # Stub the genes row only — exactly what compress_to_euchromatin
            # does (content replaced, complement/tier the summary survives).
            conn.execute(
                "UPDATE genes SET content = ?, compression_tier = 1, "
                "chromatin = 1 WHERE gene_id = ?",
                (self.STUB_TEXT, "stubgene00000001"),
            )
            conn.commit()
        finally:
            conn.close()
        return db, "stubgene00000001"

    def test_refuses_stubbed_store_without_flag(self, tmp_path):
        db, _ = self._stubbed_legacy_store(tmp_path)
        report = self._migrate(db)
        assert report["status"] == "refused_stub_loss"
        assert report["stub_guard"]["stub_rows"] == 1
        assert report["stub_guard"]["stub_rows_marker"] == 1
        assert report["stub_guard"]["stub_rows_tier1"] == 1
        assert "PERMANENTLY LOST" in report["stub_warning"]
        conn = sqlite3.connect(str(db))
        try:
            assert not fts5_is_external_content(conn.cursor()), (
                "refusal must leave the legacy store untouched"
            )
            # The retained original is still recoverable from the shadow.
            rows = conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'uniqstubneedle'"
            ).fetchall()
            assert rows, "original text vanished from the legacy shadow"
        finally:
            conn.close()

    def test_dry_run_surfaces_stub_warning(self, tmp_path):
        db, _ = self._stubbed_legacy_store(tmp_path)
        report = self._migrate(db, dry_run=True)
        assert report["status"] == "dry_run"
        assert report["stub_guard"]["stub_rows"] == 1
        assert "STUB WARNING" in report["message"]
        conn = sqlite3.connect(str(db))
        try:
            assert not fts5_is_external_content(conn.cursor())
        finally:
            conn.close()

    def test_allow_stub_loss_proceeds_and_reindexes_stub_text(self, tmp_path):
        db, stub_gid = self._stubbed_legacy_store(tmp_path)
        report = self._migrate(db, allow_stub_loss=True)
        assert report["status"] == "migrated"
        assert report["stub_loss_accepted"] is True
        conn = sqlite3.connect(str(db))
        try:
            assert fts5_is_external_content(conn.cursor())
            # The documented loss: the content-only needle is gone from the
            # index (the complement/summary legitimately survives — real
            # compression keeps it); the stub marker is searchable now.
            gone = conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'uniqstubneedle'"
            ).fetchall()
            assert gone == []
            stub = conn.execute(
                "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH 'euchromatin'"
            ).fetchall()
            assert [r[0] for r in stub] == [stub_gid]
            assert _integrity_ok(conn)
        finally:
            conn.close()

    def test_stub_free_store_needs_no_flag(self, tmp_path):
        db = tmp_path / "genome.db"
        _build_store(db)
        _downgrade_to_legacy(db)
        report = self._migrate(db)
        assert report["status"] == "migrated"
        assert report["stub_guard"]["stub_rows"] == 0
        assert "stub_warning" not in report


# ---------------------------------------------------------------------------
# 5. Open-time drift heal on external stores
# ---------------------------------------------------------------------------

class TestExternalDriftHeal:
    def test_missing_index_rows_healed_at_open(self, tmp_path):
        """A gene row committed without its FTS insert (soft-fail path) must
        be back-filled into the index on the next open."""
        db = tmp_path / "genome.db"
        _build_store(db)

        conn = sqlite3.connect(str(db))
        try:
            # Simulate the drift: index entry removed, gene row kept.
            row = conn.execute(
                f"SELECT rowid, gene_id, content, complement FROM {FTS_SOURCE_VIEW} "
                "LIMIT 1"
            ).fetchone()
            conn.execute(
                "INSERT INTO genes_fts(genes_fts, rowid, gene_id, content, complement) "
                "VALUES ('delete', ?, ?, ?, ?)",
                tuple(row),
            )
            conn.commit()
            missing = conn.execute(
                "SELECT COUNT(*) FROM genes_fts_docsize"
            ).fetchone()[0]
            assert missing == len(DOCS) - 1
        finally:
            conn.close()

        g = Genome(path=str(db), synonym_map={})
        try:
            healed = g.conn.execute(
                "SELECT COUNT(*) FROM genes_fts_docsize"
            ).fetchone()[0]
            assert healed == len(DOCS), "open-time sync did not heal the index"
            assert _integrity_ok(g.conn)
        finally:
            g.close()
