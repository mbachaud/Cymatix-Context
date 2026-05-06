"""WAL-mode behavior on the file-backed Genome.

Regression tests for the bloat fix in fix/wal-bloat-reader-snapshot:
the dedicated read-only connection must not pin a WAL snapshot that
prevents wal_checkpoint(TRUNCATE) from advancing.
"""

from __future__ import annotations

import os

import pytest

from helix_context.genome import Genome
from tests.conftest import make_gene


def _file_genome(tmp_path) -> Genome:
    return Genome(path=str(tmp_path / "genome.db"), synonym_map={})


class TestWalCheckpointAfterReads:
    def test_truncate_checkpoint_succeeds_after_reader_select(self, tmp_path):
        """After a SELECT on the dedicated reader, a TRUNCATE checkpoint
        must report busy=0 (i.e., the reader did NOT pin a snapshot)."""
        g = _file_genome(tmp_path)
        try:
            g.upsert_gene(make_gene("seed gene"))
            # First read on the dedicated reader — pre-fix this would
            # start an implicit transaction that pins WAL frames.
            g.read_conn.execute("SELECT COUNT(*) FROM genes").fetchone()

            row = g.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            assert row is not None, "wal_checkpoint returned no row"
            busy, log_pages, ckpt_pages = row
            assert busy == 0, (
                f"TRUNCATE checkpoint blocked by reader (busy={busy}); "
                f"reader's implicit transaction is still pinning the WAL"
            )
        finally:
            g.close()

    def test_journal_size_limit_pragma_applied(self, tmp_path):
        """The 64MB journal_size_limit must be set on the writer."""
        g = _file_genome(tmp_path)
        try:
            row = g.conn.execute("PRAGMA journal_size_limit").fetchone()
            assert row is not None
            assert row[0] == 67108864, (
                f"expected journal_size_limit=67108864, got {row[0]}"
            )
        finally:
            g.close()

    def test_wal_size_stays_bounded_under_repeated_writes(self, tmp_path):
        """Sanity: writes + reads + checkpoints keep the WAL file under
        the configured limit. Pre-fix, the WAL would grow without bound
        because checkpoints couldn't truncate past the reader snapshot."""
        g = _file_genome(tmp_path)
        try:
            wal_path = str(tmp_path / "genome.db-wal")
            for i in range(50):
                g.upsert_gene(make_gene(f"gene number {i}"))
                # interleave a read so the reader has a fresh statement
                g.read_conn.execute("SELECT COUNT(*) FROM genes").fetchone()
                if i % 10 == 9:
                    g.checkpoint("TRUNCATE")
            wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
            # 1 MB ceiling — generous; pre-fix bloats well past this on
            # the same workload because the reader pins frames.
            assert wal_size < 1_000_000, (
                f"WAL file grew to {wal_size:,} bytes — checkpoint is "
                f"failing to advance (reader likely still pinning a snapshot)"
            )
        finally:
            g.close()
