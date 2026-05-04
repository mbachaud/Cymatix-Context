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
from dataclasses import dataclass, field
from typing import Mapping, Optional

log = logging.getLogger("helix.hardware")


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
    """Stub — filled in by Tasks 3-10. Returns a synthetic CPU info for now
    so Task 2's tests pass."""
    return HardwareInfo(
        device="cpu",
        device_type="cpu",
        device_name="unknown CPU",
        vram_total_gb=None,
        vram_free_gb=None,
        cpu_arch="unknown",
        cpu_brand="unknown CPU",
        system_ram_gb=0.0,
        requested_device="auto",
        fallback_reason=None,
    )
