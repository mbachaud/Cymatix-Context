"""Auto-sizer helpers for parallel ingest - issue #92."""

from __future__ import annotations

from unittest.mock import patch

from cymatix_context.parallel import (
    auto_shard_file_workers,
    auto_shard_workers,
    auto_workers,
)


@patch("cymatix_context.parallel.os.cpu_count", return_value=8)
def test_auto_workers_8_core_default_buffer(_):
    """5800X-class box: 8 cores -> 6 workers (12.5% headroom)."""
    assert auto_workers() == 6


@patch("cymatix_context.parallel.os.cpu_count", return_value=16)
def test_auto_workers_16_core(_):
    """16-core box: reserves max(2, ceil(16*0.125)+1) = 3 -> 13 workers."""
    assert auto_workers() == 13


@patch("cymatix_context.parallel.os.cpu_count", return_value=4)
def test_auto_workers_4_core(_):
    """4-core box: reserves max(2, ceil(4*0.125)+1) = 2 -> 2 workers."""
    assert auto_workers() == 2


@patch("cymatix_context.parallel.os.cpu_count", return_value=2)
def test_auto_workers_2_core_floor(_):
    """2-core box: reserves 2, returns at least 1."""
    assert auto_workers() == 1


@patch("cymatix_context.parallel.os.cpu_count", return_value=None)
def test_auto_workers_handles_unknown_cpu(_):
    """os.cpu_count() can return None - fall back to 4-core assumption."""
    assert auto_workers() >= 1


class _FakeHardware:
    def __init__(self, vram: float | None, free: float | None = None):
        self.vram_total_gb = vram
        self.vram_free_gb = free


@patch("cymatix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_workers_3080ti(_):
    """12 GB VRAM + 8-core CPU: min(3 VRAM workers, 6 CPU workers) = 3."""
    with patch("cymatix_context.hardware.get_hardware",
               return_value=_FakeHardware(vram=12.0)):
        assert auto_shard_workers() == 3


@patch("cymatix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_workers_3080ti_fractional_total(_):
    """CUDA can report 11.9995 GB for a 12 GB card; that still allows 3."""
    with patch("cymatix_context.hardware.get_hardware",
               return_value=_FakeHardware(vram=11.99951171875)):
        assert auto_shard_workers() == 3


@patch("cymatix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_workers_respects_live_free_vram(_):
    """When the server is already using VRAM, auto keeps startup below OOM."""
    with patch("cymatix_context.hardware.get_hardware",
               return_value=_FakeHardware(vram=12.0, free=10.8)):
        assert auto_shard_workers() == 2


@patch("cymatix_context.parallel.os.cpu_count", return_value=16)
def test_auto_shard_workers_24gb(_):
    """24 GB + 16 core: VRAM allows 6, CPU allows 13 -> min = 6."""
    with patch("cymatix_context.hardware.get_hardware",
               return_value=_FakeHardware(vram=24.0)):
        assert auto_shard_workers() == 6


@patch("cymatix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_workers_8gb(_):
    """8 GB: VRAM caps to 2."""
    with patch("cymatix_context.hardware.get_hardware",
               return_value=_FakeHardware(vram=8.0)):
        assert auto_shard_workers() == 2


@patch("cymatix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_workers_no_gpu(_):
    """No VRAM reported -> fallback floor of 1."""
    with patch("cymatix_context.hardware.get_hardware",
               return_value=_FakeHardware(vram=None)):
        assert auto_shard_workers() == 1


@patch("cymatix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_workers_hardware_import_error(_):
    """Hardware probing raises -> still returns >= 1 (not a crash)."""
    with patch("cymatix_context.hardware.get_hardware",
               side_effect=RuntimeError("no torch")):
        assert auto_shard_workers() >= 1


@patch("cymatix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_file_workers_feeds_two_splade_workers(_):
    """5800X-class box: 2 shard workers get 3 CPU file workers each."""
    assert auto_shard_file_workers(2) == 3


@patch("cymatix_context.parallel.os.cpu_count", return_value=8)
def test_auto_shard_file_workers_feeds_three_splade_workers(_):
    """5800X-class box: 3 shard workers get 2 CPU file workers each."""
    assert auto_shard_file_workers(3) == 2
