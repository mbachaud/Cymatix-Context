"""Unit tests for helix_context.hardware (mocked-torch).

All tests use monkeypatch to mock torch internals — they never touch a real
GPU. Pattern mirrors tests/test_observability_paths.py.
"""

from __future__ import annotations

import dataclasses
import pytest

from helix_context import hardware


@pytest.fixture(autouse=True)
def _reset_hardware_cache():
    """Every test starts with a clean singleton."""
    hardware.reset_for_test()
    yield
    hardware.reset_for_test()


def test_hardware_info_is_frozen():
    info = hardware.HardwareInfo(
        device="cpu",
        device_type="cpu",
        device_name="Test CPU",
        vram_total_gb=None,
        vram_free_gb=None,
        cpu_arch="x86_64",
        cpu_brand="Test CPU",
        system_ram_gb=16.0,
        requested_device="auto",
        fallback_reason=None,
        batch_size_overrides={},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.device = "cuda:0"  # type: ignore[misc]


def test_reset_for_test_clears_cached_singleton(monkeypatch):
    """get_hardware() must recompute after reset_for_test()."""
    sentinel = hardware.HardwareInfo(
        device="cpu", device_type="cpu", device_name="Sentinel",
        vram_total_gb=None, vram_free_gb=None,
        cpu_arch="x86_64", cpu_brand="Sentinel CPU",
        system_ram_gb=16.0, requested_device="auto",
        fallback_reason=None, batch_size_overrides={},
    )
    monkeypatch.setattr(hardware, "_detect", lambda: sentinel)
    info1 = hardware.get_hardware()
    assert info1.device_name == "Sentinel"
    monkeypatch.setattr(hardware, "_detect", lambda: dataclasses.replace(sentinel, device_name="Other"))
    info2 = hardware.get_hardware()
    assert info2.device_name == "Sentinel"  # still cached
    hardware.reset_for_test()
    info3 = hardware.get_hardware()
    assert info3.device_name == "Other"
