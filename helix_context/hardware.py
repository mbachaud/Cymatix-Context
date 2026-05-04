"""Hardware detection + device backend dispatch for helix-context.

Single source of truth for which torch device backends consult. Auto-mode
picker walks CUDA -> ROCm -> MPS -> CPU; explicit-device requests fall back
loudly to CPU on probe failure but never block. See:
  docs/specs/2026-05-04-hardware-detection-design.md

Public surface:
  HardwareInfo            -- frozen dataclass returned by get_hardware()
  get_hardware()          -- cached singleton; first call performs detection
  reset_for_test()        -- test-only cache reset

Subsequent tasks fill in detection logic; Task 2 lays the dataclass +
singleton plumbing only.
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

log = logging.getLogger("helix.hardware")


def _cpuinfo_get_info() -> Optional[Dict[str, Any]]:
    """Wrap py-cpuinfo behind a function so tests can mock it cleanly.
    Returns None if py-cpuinfo isn't installed."""
    try:
        import cpuinfo
    except ImportError:
        return None
    try:
        return cpuinfo.get_cpu_info()
    except Exception:
        log.warning("py-cpuinfo get_cpu_info() raised; falling back", exc_info=True)
        return None


def _detect_cpu() -> Dict[str, Any]:
    """Detect cpu_arch, cpu_brand, system_ram_gb. Pure dict to keep
    HardwareInfo construction in one place (in _detect)."""
    info = _cpuinfo_get_info()
    if info and info.get("brand_raw"):
        cpu_brand = info["brand_raw"]
    else:
        proc = platform.processor()
        cpu_brand = proc if proc else "unknown CPU"

    try:
        import psutil
        ram_bytes = psutil.virtual_memory().total
        system_ram_gb = ram_bytes / (1024 ** 3)
    except Exception:
        log.warning("psutil.virtual_memory() failed; system_ram_gb=0", exc_info=True)
        system_ram_gb = 0.0

    return {
        "cpu_arch": platform.machine() or "unknown",
        "cpu_brand": cpu_brand,
        "system_ram_gb": system_ram_gb,
    }


@dataclass(frozen=True)
class HardwareInfo:
    """Atomic snapshot of detected hardware. Immutable — overrides flow
    through config + env var, not setters."""

    device: str
    device_type: str
    device_name: str
    vram_total_gb: Optional[float]
    vram_free_gb: Optional[float]
    cpu_arch: str
    cpu_brand: str
    system_ram_gb: float
    requested_device: str
    fallback_reason: Optional[str]
    batch_size_overrides: Mapping[str, int] = field(default_factory=dict)


_cached_info: Optional[HardwareInfo] = None


def get_hardware() -> HardwareInfo:
    """Return the cached HardwareInfo, computing it on first call."""
    global _cached_info
    if _cached_info is None:
        _cached_info = _detect()
    return _cached_info


def reset_for_test() -> None:
    """Clear the cached HardwareInfo. Test-only."""
    global _cached_info
    _cached_info = None


def _detect() -> HardwareInfo:
    cpu = _detect_cpu()
    return HardwareInfo(
        device="cpu",
        device_type="cpu",
        device_name=cpu["cpu_brand"],
        vram_total_gb=None,
        vram_free_gb=None,
        cpu_arch=cpu["cpu_arch"],
        cpu_brand=cpu["cpu_brand"],
        system_ram_gb=cpu["system_ram_gb"],
        requested_device="auto",
        fallback_reason=None,
    )
