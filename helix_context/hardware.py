"""Hardware detection + device backend dispatch for helix-context.

Single source of truth for which torch device backends consult. Auto-mode
picker walks CUDA -> ROCm -> MPS -> CPU; explicit-device requests fall back
loudly to CPU on probe failure but never block. See:
  docs/specs/2026-05-04-hardware-detection-design.md

Public surface:
  HardwareInfo            -- frozen dataclass returned by get_hardware()
  get_hardware()          -- cached singleton; first call performs detection
  reset_for_test()        -- test-only cache reset

Subsequent tasks add detection logic incrementally.
"""

from __future__ import annotations

import logging
import os
import platform
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

log = logging.getLogger("helix.hardware")

_VALID_DEVICES = ("auto", "cuda", "rocm", "mps", "cpu")


def _resolve_requested_device() -> str:
    """Resolve the user's requested device from HELIX_DEVICE env var.
    Config plumbing is added in Task 9; for now env-var-only.

    Returns one of _VALID_DEVICES; invalid values log a warning and
    return 'auto'."""
    env_value = os.environ.get("HELIX_DEVICE")
    if env_value is None:
        return "auto"
    normalized = env_value.strip().lower()
    if normalized not in _VALID_DEVICES:
        log.warning(
            "Invalid HELIX_DEVICE=%r (valid: %s); ignoring HELIX_DEVICE and using 'auto'",
            env_value, ", ".join(_VALID_DEVICES),
        )
        return "auto"
    return normalized


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


def _probe(device_str: str) -> tuple[bool, Optional[str]]:
    """Round-trip a 1-element zero tensor through the device.

    device_str: e.g. 'cuda:0', 'cuda:1', 'mps', 'cpu'.

    Returns (True, None) on success, (False, '<ExcType>: <message>') on
    failure. Catches the 'wheel says yes but kernel launch fails' failure
    mode (stale driver, dead GPU, container without device passthrough).
    Probe is ~1ms on healthy hardware.
    """
    import torch  # local import — torch may be absent (§5.5)
    try:
        t = torch.zeros(1).to(device_str)
        _ = t.cpu()
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


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


def _detect_cuda_or_rocm(rocm: bool) -> Optional[Dict[str, Any]]:
    """Enumerate live devices, pick the most-free-VRAM one that probes
    successfully. Falls through to next-best on probe failure. Returns
    None if no devices probe successfully."""
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() == 0:
        return None
    if rocm and getattr(torch.version, "hip", None) is None:
        return None
    if not rocm and getattr(torch.version, "hip", None) is not None:
        return None

    candidates = []  # list of (free_gb, total_gb, idx)
    for idx in range(torch.cuda.device_count()):
        try:
            free_b, total_b = torch.cuda.mem_get_info(idx)
        except Exception as exc:
            log.warning("cuda:%d mem_get_info failed (treated as dead): %s", idx, exc)
            continue
        candidates.append((free_b / (1024**3), total_b / (1024**3), idx))

    if not candidates:
        return None

    candidates.sort(reverse=True)  # largest free VRAM first

    for free_gb, total_gb, idx in candidates:
        ok, reason = _probe(f"cuda:{idx}")
        if not ok:
            log.warning("cuda:%d probe failed: %s — falling through", idx, reason)
            continue
        device_type = "rocm" if rocm else "cuda"
        device_str = f"{device_type}:{idx}"
        return {
            "device": device_str,
            "device_type": device_type,
            "device_name": torch.cuda.get_device_name(idx),
            "vram_total_gb": total_gb,
            "vram_free_gb": free_gb,
        }
    return None


def _detect_mps() -> Optional[Dict[str, Any]]:
    import torch
    if not (torch.backends.mps.is_available() and torch.backends.mps.is_built()):
        return None
    ok, reason = _probe("mps")
    if not ok:
        log.warning("MPS probe failed: %s", reason)
        return None
    return {
        "device": "mps",
        "device_type": "mps",
        "device_name": "Apple Silicon (MPS)",
        "vram_total_gb": None,
        "vram_free_gb": None,
    }


def _detect() -> HardwareInfo:
    cpu = _detect_cpu()
    requested = _resolve_requested_device()

    if requested == "auto":
        attempts = [
            ("cuda", lambda: _detect_cuda_or_rocm(rocm=False)),
            ("rocm", lambda: _detect_cuda_or_rocm(rocm=True)),
            ("mps",  _detect_mps),
        ]
        explicit = False
    elif requested == "cpu":
        attempts = []
        explicit = True
    elif requested == "cuda":
        attempts = [("cuda", lambda: _detect_cuda_or_rocm(rocm=False))]
        explicit = True
    elif requested == "rocm":
        attempts = [("rocm", lambda: _detect_cuda_or_rocm(rocm=True))]
        explicit = True
    elif requested == "mps":
        attempts = [("mps", _detect_mps)]
        explicit = True
    else:
        attempts = []
        explicit = True

    last_failure_reason: Optional[str] = None
    for label, fn in attempts:
        try:
            d = fn()
        except Exception as exc:
            log.warning("hardware candidate %s failed", label, exc_info=True)
            last_failure_reason = f"{label} candidate raised: {exc}"
            continue
        if d is not None:
            return HardwareInfo(
                device=d["device"],
                device_type=d["device_type"],
                device_name=d["device_name"],
                vram_total_gb=d["vram_total_gb"],
                vram_free_gb=d["vram_free_gb"],
                cpu_arch=cpu["cpu_arch"],
                cpu_brand=cpu["cpu_brand"],
                system_ram_gb=cpu["system_ram_gb"],
                requested_device=requested,
                fallback_reason=None,
            )
        else:
            last_failure_reason = (
                last_failure_reason
                or f"{label} not available (is_available()/probe returned False)"
            )

    if requested != "auto" and requested != "cpu":
        fallback_reason = (
            f"requested {requested!r} but probe/availability failed: "
            f"{last_failure_reason or 'no usable device found'}"
        )
    else:
        fallback_reason = None

    return HardwareInfo(
        device="cpu",
        device_type="cpu",
        device_name=cpu["cpu_brand"],
        vram_total_gb=None,
        vram_free_gb=None,
        cpu_arch=cpu["cpu_arch"],
        cpu_brand=cpu["cpu_brand"],
        system_ram_gb=cpu["system_ram_gb"],
        requested_device=requested,
        fallback_reason=fallback_reason,
    )
