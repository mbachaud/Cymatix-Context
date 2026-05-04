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


def test_cpu_brand_from_py_cpuinfo(monkeypatch):
    """When py-cpuinfo is installed and returns brand_raw, use it."""
    fake_cpuinfo = {"brand_raw": "AMD Ryzen 9 7900X 12-Core Processor"}
    monkeypatch.setattr(
        "helix_context.hardware._cpuinfo_get_info",
        lambda: fake_cpuinfo,
    )
    monkeypatch.setattr("platform.processor", lambda: "should not be used")
    info = hardware._detect_cpu()
    assert info["cpu_brand"] == "AMD Ryzen 9 7900X 12-Core Processor"


def test_cpu_brand_falls_back_to_platform_processor(monkeypatch):
    """When py-cpuinfo is absent, fall back to platform.processor()."""
    monkeypatch.setattr("helix_context.hardware._cpuinfo_get_info", lambda: None)
    monkeypatch.setattr("platform.processor", lambda: "Intel64 Family 6 Model 158")
    info = hardware._detect_cpu()
    assert info["cpu_brand"] == "Intel64 Family 6 Model 158"


def test_cpu_brand_terminal_fallback(monkeypatch):
    """When both sources fail, return 'unknown CPU' (no crash)."""
    monkeypatch.setattr("helix_context.hardware._cpuinfo_get_info", lambda: None)
    monkeypatch.setattr("platform.processor", lambda: "")
    info = hardware._detect_cpu()
    assert info["cpu_brand"] == "unknown CPU"


def test_cpu_arch_uses_platform_machine(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    info = hardware._detect_cpu()
    assert info["cpu_arch"] == "x86_64"


def test_system_ram_via_psutil(monkeypatch):
    class _FakeVM:
        total = 64 * 1024 * 1024 * 1024  # 64 GiB
    monkeypatch.setattr("psutil.virtual_memory", lambda: _FakeVM())
    info = hardware._detect_cpu()
    assert info["system_ram_gb"] == pytest.approx(64.0, rel=1e-3)
