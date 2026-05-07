"""Tests for VaultManager — the public API for the vault package."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from helix_context.config import HelixConfig, VaultConfig, VaultTracesConfig
from helix_context.genome import Genome
from helix_context.vault import VaultManager


@pytest.fixture
def cfg(tmp_path: Path) -> HelixConfig:
    c = HelixConfig()
    c.vault = VaultConfig(
        enabled=True, path=str(tmp_path / "vault"),
        party_id="", fan_out_threshold=5000,
        redact_body=False, stale_threshold=0.5,
        traces=VaultTracesConfig(
            enabled=True, retention_hours=48,
            max_retention_hours_hard=720,
            max_count=10000, rollup_enabled=True,
            rollup_shard="hour", prune_interval_minutes=60,
            trigger_only=False,
        ),
    )
    return c


@pytest.fixture
def genome(tmp_path: Path) -> Genome:
    g = Genome(path=str(tmp_path / "genome.db"), synonym_map={})
    yield g
    g.close()


def test_disabled_vault_does_nothing(tmp_path: Path, genome):
    cfg = HelixConfig()
    cfg.vault = VaultConfig(enabled=False, path=str(tmp_path / "vault"))
    vm = VaultManager(config=cfg, genome=genome)
    vm.start()
    assert not (tmp_path / "vault").exists()
    vm.stop()


def test_start_creates_vault_root(cfg, genome):
    vm = VaultManager(config=cfg, genome=genome)
    vm.start()
    try:
        assert Path(cfg.vault.path).exists()
        import sys
        if sys.platform != "win32":
            # Windows ignores the mode= argument to mkdir(); skip the check there.
            import stat
            mode = Path(cfg.vault.path).stat().st_mode & 0o777
            assert mode in (0o700, 0o755, 0o750), f"got mode {oct(mode)}"
    finally:
        vm.stop()


def test_stale_sentinel_cleaned_at_startup(cfg, genome):
    Path(cfg.vault.path).mkdir(parents=True, exist_ok=True, mode=0o700)
    sentinel = Path(cfg.vault.path) / ".helix-syncing"
    sentinel.touch()
    vm = VaultManager(config=cfg, genome=genome)
    vm.start()
    try:
        assert not sentinel.exists()
    finally:
        vm.stop()


def test_full_export_method(cfg, genome):
    from tests.conftest import make_gene
    from helix_context.schemas import ChromatinState

    g = make_gene("hello", domains=["auth"], chromatin=ChromatinState.EUCHROMATIN)
    g.source_id = "x.py"
    genome.upsert_gene(g)

    vm = VaultManager(config=cfg, genome=genome)
    vm.start()
    try:
        stats = vm.full_export()
        assert stats["genes_exported"] == 1
    finally:
        vm.stop()


def test_trace_export_method(cfg, genome):
    vm = VaultManager(config=cfg, genome=genome)
    vm.start()
    try:
        path = vm.trace_export(
            request_id="x",
            trigger_reason="auto",
            total_latency_ms=100,
            health_status="aligned",
            stage_timing_ms={"extract": 1},
            fingerprint_route="",
            foveated_ranks="",
            final_genes=[],
        )
        assert path.exists()
        assert "_exp" in path.name
    finally:
        vm.stop()
