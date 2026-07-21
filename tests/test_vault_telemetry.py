"""Tests for vault OTel instrument factories — caching + noop safety."""
from __future__ import annotations


def test_vault_export_histogram_caches():
    from cymatix_context.telemetry import vault_export_histogram
    h1 = vault_export_histogram()
    h2 = vault_export_histogram()
    assert h1 is h2


def test_vault_pruner_histogram_caches():
    from cymatix_context.telemetry import vault_pruner_histogram
    h1 = vault_pruner_histogram()
    h2 = vault_pruner_histogram()
    assert h1 is h2


def test_vault_force_prune_counter_caches():
    from cymatix_context.telemetry import vault_force_prune_counter
    c1 = vault_force_prune_counter()
    c2 = vault_force_prune_counter()
    assert c1 is c2


def test_vault_file_count_gauge_caches():
    from cymatix_context.telemetry import vault_file_count_gauge
    g1 = vault_file_count_gauge()
    g2 = vault_file_count_gauge()
    assert g1 is g2


def test_record_does_not_crash_in_noop_mode():
    """If OTel isn't installed the factories return _NoopInstrument; .record() is safe."""
    from cymatix_context.telemetry import (
        vault_export_histogram, vault_pruner_histogram,
        vault_force_prune_counter,
    )
    vault_export_histogram().record(0.5, {"kind": "full"})
    vault_pruner_histogram().record(0.1, {})
    vault_force_prune_counter().add(1, {"reason": "max_retention_hard"})
