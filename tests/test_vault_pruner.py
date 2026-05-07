"""Tests for the vault pruner — TTL via filename, rollup, _stale/ refresh."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from helix_context.vault.pruner import prune_traces, refresh_stale_view, migrate_fan_out_if_needed


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "_traces").mkdir(parents=True, mode=0o700)
    (root / "_traces-pinned").mkdir(parents=True, mode=0o700)
    (root / "_meta" / "trace-rollups").mkdir(parents=True, mode=0o700)
    return root


def _write_trace(vault_root: Path, *, name: str, content: str = "x") -> Path:
    p = vault_root / "_traces" / name
    p.write_text(content)
    return p


class TestPruneByFilenameSuffix:
    def test_deletes_expired(self, vault_root):
        past = int(time.time()) - 100
        f = _write_trace(vault_root, name=f"2026-01-01T00-00-00_abc_exp{past}.md")
        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["pruned_count"] == 1
        assert not f.exists()

    def test_keeps_unexpired(self, vault_root):
        future = int(time.time()) + 3600
        f = _write_trace(vault_root, name=f"2026-01-01T00-00-00_abc_exp{future}.md")
        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["pruned_count"] == 0
        assert f.exists()

    def test_skips_pinned_folder(self, vault_root):
        past = int(time.time()) - 100
        p = vault_root / "_traces-pinned" / f"2026-01-01T00-00-00_abc_exp{past}.md"
        p.write_text("x")
        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["pruned_count"] == 0
        assert p.exists()

    def test_corrupt_filename_falls_back_to_mtime(self, vault_root):
        f = _write_trace(vault_root, name="corrupt-no-exp-suffix.md")
        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["pruned_count"] == 0
        assert f.exists()

    def test_corrupt_filename_old_mtime_pruned(self, vault_root):
        f = _write_trace(vault_root, name="corrupt-no-exp.md")
        old = time.time() - 31 * 86400
        import os
        os.utime(f, (old, old))
        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["pruned_count"] == 1


class TestForcePruneHardCap:
    def test_pinned_force_pruned_past_hard_cap(self, vault_root):
        p = vault_root / "_traces-pinned" / "2026-01-01T00-00-00_abc.md"
        p.write_text("x")
        old = time.time() - 1000 * 3600  # well past 720h
        import os
        os.utime(p, (old, old))

        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["force_pruned_count"] == 1
        assert not p.exists()

    def test_disabled_when_zero(self, vault_root):
        p = vault_root / "_traces-pinned" / "2026-01-01T00-00-00_abc.md"
        p.write_text("x")
        old = time.time() - 1_000_000
        import os
        os.utime(p, (old, old))

        result = prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=0,  # disabled
            rollup_enabled=False,
            rollup_shard="hour",
        )
        assert result["force_pruned_count"] == 0
        assert p.exists()


class TestRollupAppend:
    def test_creates_hour_sharded_file(self, vault_root):
        past = int(time.time()) - 100
        content = (
            "---\n"
            "request_id: req1\n"
            "created_at: '2026-05-06T14:23:00Z'\n"
            "expires_at: '2026-05-06T14:23:00Z'\n"
            "total_latency_ms: 5000\n"
            "health_status: aligned\n"
            "trigger_reason: auto\n"
            "pinned: false\n"
            "---\n\nbody\n"
        )
        (vault_root / "_traces" / f"2026-05-06T14-23-00_req1_exp{past}.md").write_text(content)

        prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=True,
            rollup_shard="hour",
        )
        rollup = vault_root / "_meta" / "trace-rollups" / "2026-05-06" / "14.md"
        assert rollup.exists()
        text = rollup.read_text()
        assert "req1" in text
        assert "5000" in text
        assert "aligned" in text

    def test_appends_to_existing_file(self, vault_root):
        d = vault_root / "_meta" / "trace-rollups" / "2026-05-06"
        d.mkdir(parents=True)
        (d / "14.md").write_text("# Existing rollup\n\n| time | id |\n|---|---|\n| previous | yes |\n")

        past = int(time.time()) - 100
        content = (
            "---\n"
            "request_id: req2\n"
            "created_at: '2026-05-06T14:55:00Z'\n"
            "expires_at: '2026-05-06T14:55:00Z'\n"
            "total_latency_ms: 100\n"
            "health_status: sparse\n"
            "trigger_reason: latency_outlier\n"
            "pinned: false\n"
            "---\n\nbody\n"
        )
        (vault_root / "_traces" / f"2026-05-06T14-55-00_req2_exp{past}.md").write_text(content)

        prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=True,
            rollup_shard="hour",
        )
        rollup = (vault_root / "_meta" / "trace-rollups" / "2026-05-06" / "14.md")
        text = rollup.read_text()
        assert "previous" in text
        assert "req2" in text

    def test_daily_shard(self, vault_root):
        past = int(time.time()) - 100
        content = (
            "---\n"
            "request_id: req3\n"
            "created_at: '2026-05-06T14:00:00Z'\n"
            "expires_at: '2026-05-06T14:00:00Z'\n"
            "total_latency_ms: 0\n"
            "health_status: aligned\n"
            "trigger_reason: auto\n"
            "pinned: false\n"
            "---\n\nbody\n"
        )
        (vault_root / "_traces" / f"2026-05-06T14-00-00_req3_exp{past}.md").write_text(content)

        prune_traces(
            vault_root=vault_root,
            max_retention_hours_hard=720,
            rollup_enabled=True,
            rollup_shard="daily",
        )
        rollup = vault_root / "_meta" / "trace-rollups" / "2026-05-06.md"
        assert rollup.exists()
        assert "req3" in rollup.read_text()


class TestRefreshStaleView:
    def test_creates_pointer_for_stale_gene(self, tmp_path: Path):
        from helix_context.genome import Genome
        from helix_context.schemas import ChromatinState
        from tests.conftest import make_gene

        vault = tmp_path / "vault"
        vault.mkdir(mode=0o700)
        g = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
        try:
            gene = make_gene("hi", domains=["auth"], chromatin=ChromatinState.EUCHROMATIN)
            gene.source_id = "auth/x.py"
            gid = g.upsert_gene(gene)
            g.conn.execute(
                "UPDATE genes SET live_truth_score = 0.1 WHERE gene_id = ?", (gid,)
            )
            g.conn.commit()

            result = refresh_stale_view(
                vault_root=vault,
                genome=g,
                stale_threshold=0.5,
                party_id="",
            )
            assert result["added"] == 1
            assert result["errors"] == 0
            stale_files = list((vault / "_stale").iterdir())
            assert len(stale_files) == 1
            assert stale_files[0].name.endswith(".md")
            assert gid[:6] in stale_files[0].name
        finally:
            g.close()

    def test_removes_obsolete_stale_pointer(self, tmp_path: Path):
        from helix_context.genome import Genome
        from helix_context.schemas import ChromatinState
        from tests.conftest import make_gene

        vault = tmp_path / "vault"
        vault.mkdir(mode=0o700)
        stale_dir = vault / "_stale"
        stale_dir.mkdir(mode=0o700)
        # Pre-populate with a stale pointer for a gene that no longer qualifies
        (stale_dir / "old-gene-aabbcc.md").write_text("# old\n", encoding="utf-8")

        g = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
        try:
            # Insert a gene with live_truth_score >= threshold (not stale)
            gene = make_gene("healthy", domains=["auth"], chromatin=ChromatinState.EUCHROMATIN)
            gene.source_id = "auth/y.py"
            gid = g.upsert_gene(gene)
            # live_truth_score defaults to 1.0 — above 0.5 threshold

            result = refresh_stale_view(
                vault_root=vault,
                genome=g,
                stale_threshold=0.5,
                party_id="",
            )
            # The old pointer should be removed; no new ones added
            assert result["removed"] == 1
            assert result["added"] == 0
            assert not (stale_dir / "old-gene-aabbcc.md").exists()
        finally:
            g.close()


class TestFanOutMigration:
    def test_migrates_when_threshold_crossed(self, tmp_path: Path):
        from helix_context.vault.state import VaultState

        vault = tmp_path / "vault"
        vault.mkdir(mode=0o700)
        genes_core = vault / "genes" / "core"
        genes_core.mkdir(parents=True, mode=0o700)

        state = VaultState(vault_root=vault)
        try:
            # Write 5 flat files (exceeds fan_out_threshold=3)
            for i in range(5):
                fname = f"file{i}-abc{i:03d}.md"
                (genes_core / fname).write_text(f"gene {i}\n", encoding="utf-8")
                state.upsert_record(
                    gene_id=f"abc{i:03d}xxx",
                    path=f"genes/core/{fname}",
                    ts=1.0,
                    disk_hash=None,
                )

            result = migrate_fan_out_if_needed(
                vault_root=vault,
                state=state,
                fan_out_threshold=3,
            )
            assert "core" in result["migrated_domains"]
            assert result["files_migrated"] == 5

            # Files should now live under first2 subdirs
            flat = [p for p in genes_core.iterdir() if p.is_file()]
            assert len(flat) == 0, "No files should remain flat after migration"

            # Verify one specific migration: abc000 → genes/core/ab/file0-abc000.md
            assert (genes_core / "ab" / "file0-abc000.md").exists()
        finally:
            state.close()

    def test_no_migration_below_threshold(self, tmp_path: Path):
        from helix_context.vault.state import VaultState

        vault = tmp_path / "vault"
        vault.mkdir(mode=0o700)
        genes_core = vault / "genes" / "core"
        genes_core.mkdir(parents=True, mode=0o700)

        state = VaultState(vault_root=vault)
        try:
            # Write only 2 flat files (below fan_out_threshold=3)
            for i in range(2):
                fname = f"file{i}-abc{i:03d}.md"
                (genes_core / fname).write_text(f"gene {i}\n", encoding="utf-8")

            result = migrate_fan_out_if_needed(
                vault_root=vault,
                state=state,
                fan_out_threshold=3,
            )
            assert result["migrated_domains"] == []
            assert result["files_migrated"] == 0

            # Files stay flat
            flat = [p for p in genes_core.iterdir() if p.is_file()]
            assert len(flat) == 2
        finally:
            state.close()
