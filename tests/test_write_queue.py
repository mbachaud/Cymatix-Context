"""GenomeWriter (cymatix_context.write_queue) batch-writer tests.

The write_queue module has no in-repo importers — it is a standalone
single-writer seam kept for multi-agent deployments — but PR #346 wired
the issue-#338 FTS5 external-content discipline into it:

  * write_queue detects the ``genes_fts`` form once at writer-thread
    startup via ``storage.ddl.fts5_is_external_content``;
  * on the external-content form, a gene re-upsert captures the PRIOR
    view row (``storage.indexes.fetch_fts_source_row``) before mutating
    ``genes`` / ``promoter_index``, then swaps the index entry via
    ``fts_delete_row`` + ``fts_insert_from_source``;
  * on the legacy contentful form it keeps the plain
    ``INSERT OR REPLACE INTO genes_fts`` write.

These tests import the module (guarding it against zero-importer rot)
and exercise both branches against real temp genome DBs.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cymatix_context.genome import Genome
from cymatix_context.storage.ddl import FTS_SOURCE_VIEW, fts5_is_external_content
from cymatix_context.write_queue import GenomeWriter
from tests.conftest import make_gene


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DOCS = [
    ("retrieval fusion ranks candidates by reciprocal rank", ["retrieval"], ["rrf"]),
    ("the splice stage compresses candidate fragments", ["pipeline"], ["splice"]),
    ("sqlite fts5 index stores tokenized postings", ["storage"], ["fts5"]),
]

FUTURE_TIMEOUT = 15  # seconds — generous for a CI box, tiny in practice


def _build_store(path: Path) -> list[str]:
    """Create a real store at *path* (external-content form) with DOCS."""
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


def _downgrade_to_legacy(path: Path) -> None:
    """Rebuild *path*'s genes_fts in the pre-#338 plain contentful form.

    Mirrors tests/test_fts5_external_content.py::_downgrade_to_legacy so
    this file stays self-contained.
    """
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
        conn.commit()
    finally:
        conn.close()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _gene_columns(path: Path) -> list[str]:
    conn = _connect(path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(genes)").fetchall()]
    finally:
        conn.close()


def _gene_values(
    columns: list[str],
    gene_id: str,
    content: str,
    complement: str = "",
    source_id: str = "project/write_queue_test.py",
) -> tuple:
    """Column-ordered value tuple for ``INSERT OR REPLACE INTO genes``.

    Built from PRAGMA table_info so the tuple arity tracks schema
    migrations (write_queue derives its placeholder list from len(values)).
    """
    defaults = {
        "gene_id": gene_id,
        "content": content,
        "complement": complement,
        "codons": "[]",
        "promoter": "{}",
        "epigenetics": "{}",
        "chromatin": 0,
        "is_fragment": 0,
        "source_id": source_id,
        "version": 1,
        "compression_tier": 0,
        "live_truth_score": 1.0,
    }
    return tuple(defaults.get(c) for c in columns)


def _match_ids(conn: sqlite3.Connection, term: str) -> list[str]:
    rows = conn.execute(
        "SELECT gene_id FROM genes_fts WHERE genes_fts MATCH ?", (term,)
    ).fetchall()
    return [r[0] for r in rows]


def _docsize_count(conn: sqlite3.Connection) -> int:
    """Rows in the external-content FTS *index* (not the backing view)."""
    return int(conn.execute("SELECT COUNT(*) FROM genes_fts_docsize").fetchone()[0])


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def external_db(tmp_path):
    """Real store in the (default, post-#338) external-content form."""
    db = tmp_path / "genome.db"
    _build_store(db)
    return db


@pytest.fixture
def legacy_db(tmp_path):
    """Real store downgraded to the pre-#338 plain contentful form."""
    db = tmp_path / "genome.db"
    _build_store(db)
    _downgrade_to_legacy(db)
    return db


def _make_writer(db: Path) -> GenomeWriter:
    return GenomeWriter(str(db), batch_size=8, flush_interval=0.1)


@pytest.fixture
def external_writer(external_db):
    w = _make_writer(external_db)
    yield w
    w.close()


@pytest.fixture
def legacy_writer(legacy_db):
    w = _make_writer(legacy_db)
    yield w
    w.close()


# ---------------------------------------------------------------------------
# 1. Module surface — the zero-importer guard
# ---------------------------------------------------------------------------


class TestModuleSurface:
    def test_public_api_present(self):
        """write_queue has no in-repo importers; this import IS the guard
        that keeps the module (and its #346 FTS5 discipline) compiling."""
        assert callable(GenomeWriter)
        for name in ("submit_sql", "submit_gene_row", "flush", "close"):
            assert callable(getattr(GenomeWriter, name)), name

    def test_writer_starts_and_stops_cleanly(self, tmp_path):
        db = tmp_path / "genome.db"
        _build_store(db)
        w = GenomeWriter(str(db))
        assert w.pending == 0
        w.close()
        assert not w._thread.is_alive()


# ---------------------------------------------------------------------------
# 2. External-content form (the PR #346 discipline)
# ---------------------------------------------------------------------------


class TestExternalContentPath:
    def test_detects_external_form(self, external_db, external_writer):
        external_writer.flush()  # writer loop ran → detection completed
        assert external_writer._fts_external is True
        conn = _connect(external_db)
        try:
            assert fts5_is_external_content(conn.cursor())
        finally:
            conn.close()

    def test_insert_new_gene_indexes_via_source_view(
        self, external_db, external_writer
    ):
        cols = _gene_columns(external_db)
        gid = "wq-ext-new-00001"
        fut = external_writer.submit_gene_row(
            _gene_values(cols, gid, "uniqwqterm alpha payload"),
            [(gid, "domain", "writequeue"), (gid, "entity", "wqentity")],
            fts_values=(gid, "uniqwqterm alpha payload", ""),
        )
        assert fut.result(timeout=FUTURE_TIMEOUT) == gid
        external_writer.flush()

        conn = _connect(external_db)
        try:
            row = conn.execute(
                "SELECT content FROM genes WHERE gene_id = ?", (gid,)
            ).fetchone()
            assert row is not None and "uniqwqterm" in row[0]
            tags = conn.execute(
                "SELECT tag_type, tag_value FROM promoter_index "
                "WHERE gene_id = ? ORDER BY rowid",
                (gid,),
            ).fetchall()
            assert [tuple(t) for t in tags] == [
                ("domain", "writequeue"),
                ("entity", "wqentity"),
            ]
            # Content term searchable through the index...
            assert _match_ids(conn, "uniqwqterm") == [gid]
            # ...and so are the view-composited tag tokens (proof the row
            # was indexed from the backing view, not the raw fts_values).
            assert gid in _match_ids(conn, "wqentity")
            assert _docsize_count(conn) == len(DOCS) + 1
            assert _integrity_ok(conn)
        finally:
            conn.close()

    def test_reupsert_swaps_index_entry_without_stale_terms(
        self, external_db, external_writer
    ):
        """The headline #338 branch: prior view row captured before the
        REPLACE (fetch_fts_source_row), old postings removed with the
        prior values (fts_delete_row), current view row re-indexed
        (fts_insert_from_source)."""
        cols = _gene_columns(external_db)
        gid = "wq-ext-reup-0001"
        first = external_writer.submit_gene_row(
            _gene_values(cols, gid, "obsoletewqterm bravo payload"),
            [(gid, "domain", "writequeue")],
            fts_values=(gid, "obsoletewqterm bravo payload", ""),
        )
        assert first.result(timeout=FUTURE_TIMEOUT) == gid
        external_writer.flush()

        second = external_writer.submit_gene_row(
            _gene_values(cols, gid, "freshwqterm charlie payload"),
            [(gid, "domain", "writequeue")],
            fts_values=(gid, "freshwqterm charlie payload", ""),
        )
        assert second.result(timeout=FUTURE_TIMEOUT) == gid
        external_writer.flush()

        conn = _connect(external_db)
        try:
            assert _match_ids(conn, "obsoletewqterm") == [], (
                "stale terms still searchable after re-upsert"
            )
            assert _match_ids(conn, "freshwqterm") == [gid]
            # Exactly one index row for the gene — no duplicate postings.
            assert _docsize_count(conn) == len(DOCS) + 1
            assert _integrity_ok(conn)
        finally:
            conn.close()

    def test_no_fts_values_skips_index(self, external_db, external_writer):
        """fts_values=None writes the gene row only — the index is not
        touched (the caller opted out of FTS sync)."""
        cols = _gene_columns(external_db)
        gid = "wq-ext-nofts-001"
        fut = external_writer.submit_gene_row(
            _gene_values(cols, gid, "unindexedwqterm delta payload"),
            [],
            fts_values=None,
        )
        assert fut.result(timeout=FUTURE_TIMEOUT) == gid
        external_writer.flush()

        conn = _connect(external_db)
        try:
            assert conn.execute(
                "SELECT 1 FROM genes WHERE gene_id = ?", (gid,)
            ).fetchone() is not None
            assert _match_ids(conn, "unindexedwqterm") == []
            assert _docsize_count(conn) == len(DOCS)
        finally:
            conn.close()

    def test_reupsert_of_drifted_gene_heals_without_bogus_delete(
        self, external_db, external_writer
    ):
        """A gene present in ``genes`` but absent from the index (drift)
        re-upserts cleanly: fts_delete_row's docsize guard sees the rowid
        was never indexed, skips the corrupting 'delete', and the row is
        then indexed from the view."""
        cols = _gene_columns(external_db)
        gid = "wq-ext-drift-001"
        # Step 1: create the drift — gene row without an index entry.
        fut = external_writer.submit_gene_row(
            _gene_values(cols, gid, "driftwqterm echo payload"),
            [],
            fts_values=None,
        )
        assert fut.result(timeout=FUTURE_TIMEOUT) == gid
        external_writer.flush()

        # Step 2: re-upsert WITH fts_values — prior view row exists (the
        # view reproduces every genes row) but the index has no posting.
        fut2 = external_writer.submit_gene_row(
            _gene_values(cols, gid, "healedwqterm foxtrot payload"),
            [],
            fts_values=(gid, "healedwqterm foxtrot payload", ""),
        )
        assert fut2.result(timeout=FUTURE_TIMEOUT) == gid
        external_writer.flush()

        conn = _connect(external_db)
        try:
            assert _match_ids(conn, "healedwqterm") == [gid]
            assert _docsize_count(conn) == len(DOCS) + 1
            assert _integrity_ok(conn)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 3. Legacy contentful form
# ---------------------------------------------------------------------------


class TestLegacyPath:
    def test_detects_legacy_form(self, legacy_db, legacy_writer):
        legacy_writer.flush()
        assert legacy_writer._fts_external is False
        conn = _connect(legacy_db)
        try:
            assert not fts5_is_external_content(conn.cursor())
        finally:
            conn.close()

    def test_insert_uses_plain_fts_write(self, legacy_db, legacy_writer):
        """On the legacy form the submitted fts_values land verbatim via
        INSERT OR REPLACE — no view composition, no docsize discipline."""
        cols = _gene_columns(legacy_db)
        gid = "wq-leg-new-00001"
        fut = legacy_writer.submit_gene_row(
            _gene_values(cols, gid, "legacywqterm golf payload"),
            [(gid, "domain", "writequeue")],
            fts_values=(gid, "legacywqterm golf payload", "summary text"),
        )
        assert fut.result(timeout=FUTURE_TIMEOUT) == gid
        legacy_writer.flush()

        conn = _connect(legacy_db)
        try:
            assert _match_ids(conn, "legacywqterm") == [gid]
            # Verbatim fts_values, NOT the view composite: the tag token
            # was not folded into the indexed content.
            assert gid not in _match_ids(conn, "writequeue")
            row = conn.execute(
                "SELECT content, complement FROM genes_fts WHERE gene_id = ?",
                (gid,),
            ).fetchone()
            assert row == ("legacywqterm golf payload", "summary text")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 4. Queue mechanics — batching, raw SQL, errors, stats, shutdown
# ---------------------------------------------------------------------------


class TestQueueMechanics:
    def test_submit_sql_resolves_after_flush(self, external_db, external_writer):
        fut = external_writer.submit_sql(
            "INSERT INTO promoter_index VALUES (?, ?, ?)",
            ("wq-raw-sql-00001", "domain", "rawsql"),
        )
        fut.result(timeout=FUTURE_TIMEOUT)
        external_writer.flush()

        conn = _connect(external_db)
        try:
            row = conn.execute(
                "SELECT tag_value FROM promoter_index WHERE gene_id = ?",
                ("wq-raw-sql-00001",),
            ).fetchone()
            assert row == ("rawsql",)
        finally:
            conn.close()

    def test_bad_sql_fails_its_future_only(self, external_db, external_writer):
        bad = external_writer.submit_sql("INSERT INTO no_such_table VALUES (1)")
        good = external_writer.submit_sql(
            "INSERT INTO promoter_index VALUES (?, ?, ?)",
            ("wq-good-after-bad", "domain", "survivor"),
        )
        with pytest.raises(sqlite3.OperationalError):
            bad.result(timeout=FUTURE_TIMEOUT)
        good.result(timeout=FUTURE_TIMEOUT)
        external_writer.flush()

        conn = _connect(external_db)
        try:
            assert conn.execute(
                "SELECT 1 FROM promoter_index WHERE gene_id = ?",
                ("wq-good-after-bad",),
            ).fetchone() is not None
        finally:
            conn.close()

    def test_wrong_arity_gene_row_fails_its_future(
        self, external_db, external_writer
    ):
        fut = external_writer.submit_gene_row(
            ("wq-bad-arity", "only two columns"), [], fts_values=None
        )
        with pytest.raises(sqlite3.OperationalError):
            fut.result(timeout=FUTURE_TIMEOUT)

    def test_stats_count_writes_and_batches(self, external_db, external_writer):
        cols = _gene_columns(external_db)
        n = 5
        futures = [
            external_writer.submit_gene_row(
                _gene_values(cols, f"wq-stats-{i:05d}", f"statsterm{i} payload"),
                [],
                fts_values=(f"wq-stats-{i:05d}", f"statsterm{i} payload", ""),
            )
            for i in range(n)
        ]
        for fut in futures:
            fut.result(timeout=FUTURE_TIMEOUT)
        external_writer.flush()

        stats = external_writer.stats
        assert stats["total_writes"] == n
        assert stats["total_batches"] >= 1
        assert stats["errors"] == 0

    def test_close_drains_pending_writes(self, external_db):
        cols = _gene_columns(external_db)
        writer = _make_writer(external_db)
        futures = [
            writer.submit_gene_row(
                _gene_values(cols, f"wq-drain-{i:05d}", f"drainterm{i} payload"),
                [],
                fts_values=(f"wq-drain-{i:05d}", f"drainterm{i} payload", ""),
            )
            for i in range(10)
        ]
        writer.close()

        for fut in futures:
            assert fut.result(timeout=FUTURE_TIMEOUT).startswith("wq-drain-")
        conn = _connect(external_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM genes WHERE gene_id LIKE 'wq-drain-%'"
            ).fetchone()[0]
            assert count == 10
        finally:
            conn.close()

    def test_concurrent_submitters_all_resolve(self, external_db, external_writer):
        cols = _gene_columns(external_db)
        per_thread, n_threads = 8, 4
        results: list[str] = []
        lock = threading.Lock()

        def submit(worker: int) -> None:
            futs = [
                external_writer.submit_gene_row(
                    _gene_values(
                        cols,
                        f"wq-thr-{worker}-{i:03d}",
                        f"threadterm{worker}x{i} payload",
                    ),
                    [],
                    fts_values=(
                        f"wq-thr-{worker}-{i:03d}",
                        f"threadterm{worker}x{i} payload",
                        "",
                    ),
                )
                for i in range(per_thread)
            ]
            resolved = [f.result(timeout=FUTURE_TIMEOUT) for f in futs]
            with lock:
                results.extend(resolved)

        threads = [
            threading.Thread(target=submit, args=(w,)) for w in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        external_writer.flush()

        assert len(results) == per_thread * n_threads
        assert len(set(results)) == per_thread * n_threads
        conn = _connect(external_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM genes WHERE gene_id LIKE 'wq-thr-%'"
            ).fetchone()[0]
            assert count == per_thread * n_threads
            assert _docsize_count(conn) == len(DOCS) + per_thread * n_threads
            assert _integrity_ok(conn)
        finally:
            conn.close()
