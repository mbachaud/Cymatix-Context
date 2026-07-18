"""
Bug-bash regression tests: replication follow-up sync + cross-store
import round-trip integrity + lifecycle-state preservation on import.

BUG-1: writes that cross the sync threshold while a sync is already in
       flight must queue a follow-up sync (persistence.py).
BUG-2: export -> import of the same archive must not reject the
       exporter's own stable-ID or compressed records as tampered
       (cross_store_import.py).
BUG-3: import must preserve the exported lifecycle tier instead of
       re-running the density gate (cross_store_import.py).
"""

import json
import sqlite3
import threading
import time

import pytest

from helix_context.cross_store_import import export_genome, import_genome
from helix_context.knowledge_store import KnowledgeStore as Genome
from helix_context.persistence import ReplicationManager
from helix_context.schemas import ChromatinState

from tests.conftest import make_gene


# ═══════════════════════════════════════════════════════════════════
# BUG-1 — follow-up sync for writes landing during an in-flight sync
# ═══════════════════════════════════════════════════════════════════


class TestReplicationFollowUpSync:
    def _make_master(self, tmp_path):
        master = tmp_path / "master.db"
        conn = sqlite3.connect(str(master))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()
        return master

    def test_threshold_writes_during_inflight_sync_requeue(self, tmp_path):
        """Writes crossing the threshold mid-sync must trigger a follow-up.

        Without the fix, notify_write() resets the counter while a sync is
        in flight and schedules nothing — those writes are counted as
        flushed but never replicated until a *further* full interval of
        writes arrives (possibly never).
        """
        master = self._make_master(tmp_path)
        replica = tmp_path / "replica.db"
        mgr = ReplicationManager(str(master), [str(replica)], sync_interval=1)

        release = threading.Event()
        backup_calls = []
        orig_backup = mgr._backup_to

        def slow_backup(src, rep):
            backup_calls.append(rep)
            release.wait(timeout=5)
            orig_backup(src, rep)

        mgr._backup_to = slow_backup

        # First threshold crossing: starts a sync that blocks in slow_backup.
        mgr.notify_write()
        deadline = time.time() + 5
        while not backup_calls and time.time() < deadline:
            time.sleep(0.01)
        assert backup_calls, "first sync never started"

        # Second threshold crossing lands while the sync is in flight.
        mgr.notify_write()

        # Let the in-flight sync finish; a follow-up sync must run.
        release.set()
        deadline = time.time() + 5
        while len(backup_calls) < 2 and time.time() < deadline:
            time.sleep(0.01)
        mgr.close()

        assert len(backup_calls) >= 2, (
            "write that crossed the sync threshold during an in-flight "
            "sync never triggered a follow-up sync"
        )

    def test_no_spurious_followup_without_pending_writes(self, tmp_path):
        """A single threshold crossing runs exactly one sync (no re-queue loop)."""
        master = self._make_master(tmp_path)
        replica = tmp_path / "replica.db"
        mgr = ReplicationManager(str(master), [str(replica)], sync_interval=1)

        backup_calls = []
        orig_backup = mgr._backup_to

        def counting_backup(src, rep):
            backup_calls.append(rep)
            orig_backup(src, rep)

        mgr._backup_to = counting_backup

        mgr.notify_write()
        deadline = time.time() + 5
        while not backup_calls and time.time() < deadline:
            time.sleep(0.01)
        # Give any (buggy) follow-up a moment to fire.
        time.sleep(0.2)
        mgr.close()

        assert len(backup_calls) == 1, (
            f"expected exactly one sync, got {len(backup_calls)}"
        )


# ═══════════════════════════════════════════════════════════════════
# BUG-2 — export/import round-trip integrity
# ═══════════════════════════════════════════════════════════════════


class TestCrossStoreRoundTrip:
    def test_stable_id_gene_roundtrips(self, tmp_path):
        """A gene with a non-content-addressed (stable) ID must survive
        its own export -> import round trip instead of being flagged
        tampered (e.g. ``presence:<participant>`` genes from
        identity/registry.py keep a stable ID while content changes)."""
        source = Genome(str(tmp_path / "source.db"))
        gene = make_gene(
            "agent presence record for red70",
            domains=["identity"], entities=["agent"],
            gene_id="presence:agent-red70",
        )
        source.upsert_doc(gene)

        path = str(tmp_path / "export.helix")
        export_genome(source, path)
        source.close()

        target = Genome(str(tmp_path / "target.db"))
        result = import_genome(target, path)
        assert result["tampered"] == 0, (
            "importer rejected the exporter's own stable-ID record as tampered"
        )
        assert result["imported"] == 1
        assert target.get_doc("presence:agent-red70") is not None
        target.close()

    def test_compressed_gene_roundtrips(self, tmp_path):
        """A euchromatin-compressed gene (content rewritten in place,
        gene_id unchanged) must survive its own export -> import round trip."""
        source = Genome(str(tmp_path / "source.db"))
        content = "detailed design notes on the splice batching pipeline"
        gene = make_gene(
            content, domains=["design"], entities=["splice"],
            chromatin=ChromatinState.EUCHROMATIN,
        )
        source.upsert_doc(gene)
        assert source.compress_to_euchromatin(gene.gene_id), (
            "fixture error: compression did not run"
        )

        path = str(tmp_path / "export.helix")
        export_genome(source, path)
        source.close()

        target = Genome(str(tmp_path / "target.db"))
        result = import_genome(target, path)
        assert result["tampered"] == 0, (
            "importer rejected the exporter's own compressed record as tampered"
        )
        assert result["imported"] == 1
        imported = target.get_doc(gene.gene_id)
        assert imported is not None
        assert imported.content.startswith("[COMPRESSED:euchromatin]")
        target.close()

    def test_edited_content_still_flagged_tampered(self, tmp_path):
        """Genuine in-transit edits must still be caught."""
        source = Genome(str(tmp_path / "source.db"))
        gene = make_gene("original trustworthy content", domains=["test"])
        source.upsert_doc(gene)

        path = str(tmp_path / "export.helix")
        export_genome(source, path)
        source.close()

        data = json.loads(open(path, encoding="utf-8").read())
        data["genes"][0]["content"] = "maliciously edited content"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        target = Genome(str(tmp_path / "target.db"))
        result = import_genome(target, path)
        assert result["tampered"] == 1
        assert result["imported"] == 0
        target.close()

    def test_legacy_file_without_checksums_still_imports(self, tmp_path):
        """Pre-checksum .helix files (content-addressed IDs) keep working."""
        source = Genome(str(tmp_path / "source.db"))
        content = "legacy content-addressed knowledge"
        gene = make_gene(content, domains=["test"])
        source.upsert_doc(gene)

        path = str(tmp_path / "export.helix")
        export_genome(source, path)
        source.close()

        # Strip any checksum section to simulate an older export.
        data = json.loads(open(path, encoding="utf-8").read())
        data.pop("gene_checksums", None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        target = Genome(str(tmp_path / "target.db"))
        result = import_genome(target, path)
        assert result["imported"] == 1
        assert result["tampered"] == 0
        assert target.get_doc(Genome.make_gene_id(content)) is not None
        target.close()


# ═══════════════════════════════════════════════════════════════════
# BUG-3 — import preserves exported lifecycle tier (no density gate)
# ═══════════════════════════════════════════════════════════════════


class TestImportPreservesLifecycleTier:
    def test_open_sparse_gene_stays_open_on_import(self, tmp_path):
        """A gene exported OPEN must land OPEN — the density gate belongs
        to original ingest, not import (see upsert_doc docstring, which
        names cross-store imports as apply_gate=False callers)."""
        source = Genome(str(tmp_path / "source.db"))
        # Sparse content: no tags, no key-values -> density score ~0.1,
        # far below the heterochromatin threshold (0.50). Kept OPEN at
        # the source by an explicit gate bypass (e.g. operator decision,
        # access-rate history that does not survive the transfer, or a
        # differently-configured deny list).
        content = (
            "plain prose notes kept deliberately open by the source "
            "operator despite carrying no promoter tags at all"
        )
        gene = make_gene(content)
        source.upsert_doc(gene, apply_gate=False)
        assert source.get_doc(gene.gene_id).chromatin == ChromatinState.OPEN

        path = str(tmp_path / "export.helix")
        export_genome(source, path)
        source.close()

        target = Genome(str(tmp_path / "target.db"))
        result = import_genome(target, path)
        assert result["imported"] == 1
        imported = target.get_doc(gene.gene_id)
        assert imported is not None
        assert imported.chromatin == ChromatinState.OPEN, (
            f"import silently changed lifecycle tier: exported OPEN, "
            f"landed {imported.chromatin!r} (density gate re-applied on import)"
        )
        target.close()
