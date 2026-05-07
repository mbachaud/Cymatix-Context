"""Tests for the vault pruner — TTL via filename, rollup, _stale/ refresh."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from helix_context.vault.pruner import prune_traces


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
