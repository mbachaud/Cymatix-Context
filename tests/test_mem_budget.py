"""Contract tests for the RAM-aware SQLite memory budget (PRD 2026-05-30).

The budget scales per-shard ``mmap_size`` / ``cache_size`` from *available* RAM
divided by shard count. These tests pin the CONTRACT (never exceed available
RAM, monotonic in RAM, throttles with shard count, exact escape hatch) rather
than the exact arithmetic, so the profile constants can be tuned without
rewriting the suite.

The plan stores the literal pragma values to emit:
  - ``mmap_size``        -> ``PRAGMA mmap_size={mmap_size}``           (bytes)
  - ``writer_cache_size``-> ``PRAGMA cache_size={writer_cache_size}`` (neg = KiB)
  - ``reader_cache_size``-> ``PRAGMA cache_size={reader_cache_size}``
"""
from __future__ import annotations

import pytest

from helix_context.hardware import SqliteMemPlan, sqlite_memory_budget

GiB = 1024 ** 3
MiB = 1024 ** 2


def _footprint_bytes(plan: SqliteMemPlan) -> int:
    """Per-shard resident bytes the plan asks SQLite for: mmap + the larger
    of the two page caches (cache_size negative = KiB)."""
    cache_kib = max(abs(plan.writer_cache_size), abs(plan.reader_cache_size))
    return plan.mmap_size + cache_kib * 1024


# ── escape hatch: conservative == exact v0.6.1 ──────────────────────────────

def test_conservative_profile_is_byte_identical_to_v061(monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "conservative")
    plan = sqlite_memory_budget(100, available_bytes=128 * GiB)
    assert plan.mmap_size == 0
    assert plan.writer_cache_size == -2048   # 2 MB writer (v0.6.1)
    assert plan.reader_cache_size == -4096   # 4 MB reader (v0.6.1)


# ── auto: the core dynamic behavior ─────────────────────────────────────────

def test_auto_enables_mmap_when_ram_is_free(monkeypatch):
    monkeypatch.delenv("HELIX_MEM_PROFILE", raising=False)  # default == auto
    plan = sqlite_memory_budget(100, available_bytes=28 * GiB)
    assert plan.mmap_size > 0, "auto must turn mmap ON when RAM is available"


def test_auto_never_exceeds_available_ram(monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")
    for avail_gib, n in [(28, 100), (110, 100), (12, 105), (48, 8), (8, 1)]:
        plan = sqlite_memory_budget(n, available_bytes=avail_gib * GiB)
        total = _footprint_bytes(plan) * n
        assert total <= avail_gib * GiB, (
            f"avail={avail_gib}GiB n={n}: footprint {total} exceeds available"
        )


def test_auto_more_ram_yields_more_mmap(monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")
    small = sqlite_memory_budget(100, available_bytes=28 * GiB)
    big = sqlite_memory_budget(100, available_bytes=110 * GiB)
    assert big.mmap_size > small.mmap_size


def test_auto_more_shards_yields_less_mmap_per_shard(monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")
    few = sqlite_memory_budget(10, available_bytes=48 * GiB)
    many = sqlite_memory_budget(100, available_bytes=48 * GiB)
    assert many.mmap_size < few.mmap_size


def test_auto_cache_within_bounds(monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")
    for avail_gib, n in [(28, 100), (110, 100), (12, 105)]:
        plan = sqlite_memory_budget(n, available_bytes=avail_gib * GiB)
        for cs in (plan.writer_cache_size, plan.reader_cache_size):
            assert cs < 0, "cache_size must stay in negative-KiB units"
            kib = abs(cs)
            assert 2 * 1024 <= kib <= 64 * 1024, f"cache {kib} KiB out of [2,64] MB"


def test_auto_mmap_has_absolute_cap(monkeypatch):
    # n_shards=1 on a huge host would otherwise map an unbounded amount.
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")
    plan = sqlite_memory_budget(1, available_bytes=110 * GiB)
    assert plan.mmap_size == 2 * GiB, "auto mmap must cap at 2 GiB/shard"


def test_auto_high_shard_pressure_throttles(monkeypatch):
    # the 105-shard / low-RAM stress case must throttle back toward off.
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")
    plan = sqlite_memory_budget(105, available_bytes=12 * GiB)
    assert plan.mmap_size < 100 * MiB


# ── degenerate inputs fall back to the safe (conservative) plan ─────────────

def test_tiny_ram_falls_back_to_conservative(monkeypatch):
    # available < reserve floor -> no budget -> behave like conservative.
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")
    plan = sqlite_memory_budget(100, available_bytes=2 * GiB)
    assert plan.mmap_size == 0


def test_psutil_failure_falls_back_to_conservative(monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")

    def _boom():
        raise RuntimeError("psutil unavailable")

    monkeypatch.setattr("psutil.virtual_memory", _boom)
    plan = sqlite_memory_budget(100)  # available_bytes=None -> probes psutil
    assert plan.mmap_size == 0
    assert plan.writer_cache_size == -2048


def test_auto_reads_available_from_psutil_when_not_passed(monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")

    class _VM:
        available = 110 * GiB

    monkeypatch.setattr("psutil.virtual_memory", lambda: _VM())
    plan = sqlite_memory_budget(100)
    assert plan.mmap_size > 0


# ── explicit overrides win over the profile ─────────────────────────────────

def test_mmap_size_env_override_wins(monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "conservative")  # would be 0
    monkeypatch.setenv("HELIX_SQLITE_MMAP_SIZE", str(123 * MiB))
    plan = sqlite_memory_budget(100, available_bytes=28 * GiB)
    assert plan.mmap_size == 123 * MiB


def test_cache_size_env_override_wins(monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")
    monkeypatch.setenv("HELIX_SQLITE_CACHE_SIZE", "-8192")  # raw pragma value
    plan = sqlite_memory_budget(100, available_bytes=28 * GiB)
    assert plan.writer_cache_size == -8192
    assert plan.reader_cache_size == -8192


# ── explicit GiB budget is independent of host available RAM ────────────────

def test_explicit_gb_budget_ignores_available(monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "8gb")
    lo = sqlite_memory_budget(100, available_bytes=16 * GiB)
    hi = sqlite_memory_budget(100, available_bytes=512 * GiB)
    assert lo == hi, "explicit budget must not vary with host available RAM"
    assert lo.mmap_size > 0


# ── aggressive grants at least as much as auto ──────────────────────────────

def test_aggressive_grants_at_least_auto(monkeypatch):
    monkeypatch.setenv("HELIX_MEM_PROFILE", "auto")
    auto = sqlite_memory_budget(100, available_bytes=110 * GiB)
    monkeypatch.setenv("HELIX_MEM_PROFILE", "aggressive")
    aggr = sqlite_memory_budget(100, available_bytes=110 * GiB)
    assert aggr.mmap_size >= auto.mmap_size
