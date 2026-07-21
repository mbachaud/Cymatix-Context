"""Hardware detection + device backend dispatch for helix-context.

Single source of truth for which torch device backends consult. Auto-mode
picker walks CUDA -> ROCm -> MPS -> CPU; explicit-device requests fall back
loudly to CPU on probe failure but never block. See:
  docs/specs/2026-05-04-hardware-detection-design.md

Public surface:
  HardwareInfo            -- frozen dataclass returned by get_hardware()
  get_hardware()          -- cached singleton; first call performs detection
  recommended_batch_size(model) -- per-model batch size from table or override
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


def _resolve_requested_device(config_device: str = "auto") -> str:
    """Resolve the user's requested device.

    Order of precedence:
      1. ``HELIX_DEVICE`` env var (operator override at launch).
      2. ``config_device`` arg (from ``[hardware] device`` in helix.toml).
      3. ``"auto"`` (sentinel; the picker walks CUDA -> ROCm -> MPS -> CPU).

    Returns one of ``_VALID_DEVICES``; invalid env/config values log a
    warning and fall through to the next layer."""
    env_value = os.environ.get("HELIX_DEVICE")
    if env_value is not None:
        normalized = env_value.strip().lower()
        if normalized in _VALID_DEVICES:
            return normalized
        log.warning(
            "Invalid HELIX_DEVICE=%r (valid: %s); ignoring HELIX_DEVICE",
            env_value, ", ".join(_VALID_DEVICES),
        )
        # fall through to config / auto
    cfg_norm = config_device.strip().lower() if isinstance(config_device, str) else ""
    if cfg_norm in _VALID_DEVICES:
        return cfg_norm
    log.warning("Invalid [hardware] device=%r; using 'auto'", config_device)
    return "auto"


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
# Config-supplied defaults consumed by ``_detect()``. ``init_from_config``
# is the only public setter; ``reset_for_test`` clears them.
_config_device: str = "auto"
_config_overrides: Mapping[str, int] = {}


def get_hardware() -> HardwareInfo:
    """Return the cached HardwareInfo, computing it on first call."""
    global _cached_info
    if _cached_info is None:
        _cached_info = _detect()
    return _cached_info


def init_from_config(
    config_device: str = "auto",
    batch_size_overrides: Optional[Mapping[str, int]] = None,
) -> HardwareInfo:
    """One-shot init at server startup.

    Sets the config-supplied ``requested_device`` and ``batch_size_overrides``
    that ``_detect()`` will stamp onto the singleton, then forces detection.

    **Idempotent on the cache**: if ``get_hardware()`` (or a previous
    ``init_from_config()``) already populated the singleton, the cached
    ``HardwareInfo`` is returned unchanged. This is the
    cached-singleton-poisoning failure mode pinned by
    ``tests/test_hardware.py::test_init_from_config_must_run_before_get_hardware``
    — the call order at server startup matters.

    Returns the (possibly-cached) ``HardwareInfo``."""
    global _cached_info, _config_device, _config_overrides
    if _cached_info is not None:
        return _cached_info
    _config_device = config_device
    _config_overrides = dict(batch_size_overrides) if batch_size_overrides else {}
    _cached_info = _detect()
    return _cached_info


def reset_for_test() -> None:
    """Clear the cached HardwareInfo + config state. Test-only."""
    global _cached_info, _config_device, _config_overrides
    _cached_info = None
    _config_device = "auto"
    _config_overrides = {}


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
    requested = _resolve_requested_device(_config_device)
    overrides = dict(_config_overrides)

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
                batch_size_overrides=overrides,
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
        # SF2 (#65): headless deployments miss the tray balloon. A summary
        # WARNING here means operators tailing logs see one clear line
        # connecting the per-candidate probe failures above to the
        # final "we ended up on CPU" outcome.
        log.warning(
            "Hardware fallback: requested=%s active=cpu — %s",
            requested, fallback_reason,
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
        batch_size_overrides=overrides,
    )


# Batch-size table — keys are (device_type, ram_threshold_gb_min).
# Lookup picks the highest threshold row whose minimum is <= the
# observed value. See spec §7.1 for calibration rationale.
_BATCH_TABLE: Dict[tuple, Dict[str, int]] = {
    # CUDA / ROCm tiers — keyed on TOTAL VRAM (invariant per GPU).
    ("cuda", 24.0): {"rerank": 64, "splice": 128, "splade": 32, "nli": 32},
    ("cuda", 12.0): {"rerank": 32, "splice": 64,  "splade": 16, "nli": 16},
    ("cuda",  8.0): {"rerank": 16, "splice": 32,  "splade":  8, "nli":  8},
    ("cuda",  4.0): {"rerank":  8, "splice": 16,  "splade":  4, "nli":  4},
    ("cuda",  0.0): {"rerank":  4, "splice":  8,  "splade":  2, "nli":  2},
    ("rocm", 24.0): {"rerank": 64, "splice": 128, "splade": 32, "nli": 32},
    ("rocm", 12.0): {"rerank": 32, "splice": 64,  "splade": 16, "nli": 16},
    ("rocm",  8.0): {"rerank": 16, "splice": 32,  "splade":  8, "nli":  8},
    ("rocm",  4.0): {"rerank":  8, "splice": 16,  "splade":  4, "nli":  4},
    ("rocm",  0.0): {"rerank":  4, "splice":  8,  "splade":  2, "nli":  2},
    # MPS — keyed on system_ram_gb (MPS shares system RAM).
    ("mps", 16.0): {"rerank": 16, "splice": 32, "splade":  8, "nli":  8},
    ("mps",  8.0): {"rerank":  8, "splice": 16, "splade":  4, "nli":  4},
    ("mps",  0.0): {"rerank":  4, "splice":  8, "splade":  2, "nli":  2},
    # CPU — keyed on system_ram_gb.
    ("cpu", 16.0): {"rerank":  8, "splice": 16, "splade":  4, "nli":  4},
    ("cpu",  8.0): {"rerank":  4, "splice":  8, "splade":  2, "nli":  2},
    ("cpu",  0.0): {"rerank":  2, "splice":  4, "splade":  1, "nli":  1},
}


def _lookup_batch_size(device_type: str, tier_value: float, model: str) -> int:
    """Find the highest-threshold row whose min <= tier_value."""
    matching = [
        (threshold, row[model])
        for (dt, threshold), row in _BATCH_TABLE.items()
        if dt == device_type and threshold <= tier_value and model in row
    ]
    if not matching:
        return 1  # ultra-conservative floor for unknown model / table miss
    matching.sort(reverse=True)
    return matching[0][1]


def recommended_batch_size(model: str) -> int:
    """Resolve batch size for `model`. Order:
    1. info.batch_size_overrides[model] if present
    2. _BATCH_TABLE lookup based on device_type + (vram_total_gb or system_ram_gb)
    3. Conservative floor (1) for unknown models.
    """
    info = get_hardware()
    if model in info.batch_size_overrides:
        return info.batch_size_overrides[model]
    if info.device_type in {"cuda", "rocm"}:
        tier = info.vram_total_gb if info.vram_total_gb is not None else 0.0
    else:
        tier = info.system_ram_gb
    return _lookup_batch_size(info.device_type, tier, model)


# ── SQLite memory budget (PRD 2026-05-30: dynamic, RAM-aware scaling) ────────
# v0.6.1 hard-coded mmap_size=0 + 2/4 MB page caches on every host as a
# 100-shard fan-out guard. With the BGE-M3 model singleton (the actual
# 120 GB -> 7 GB fix) in place, that posture over-throttles RAM-rich hosts.
# This scales per-shard mmap/cache from *available* RAM / shard count; because
# the budget is (available - reserve), it can never claim more RAM than exists.
_MEM_GiB = 1024 ** 3
_MEM_MiB = 1024 ** 2

# `conservative` profile == byte-identical to v0.6.1 (the escape hatch).
_CONSERVATIVE_MMAP = 0
_CONSERVATIVE_WRITER_CACHE = -2048   # 2 MB writer page cache
_CONSERVATIVE_READER_CACHE = -4096   # 4 MB reader page cache

# Scaling profiles: (reserve_frac, mmap_frac, mmap_cap_gib, cache_max_mib).
#   reserve_frac  fraction of available RAM held back for heap/model/dense matrix
#   mmap_frac     share of the per-shard budget given to file-backed mmap
#   mmap_cap_gib  hard per-shard mmap ceiling (SQLite maps lazily <= file size)
#   cache_max_mib upper bound on the per-shard private page cache
_MEM_PROFILES: Dict[str, tuple] = {
    "auto":       (0.25, 0.80, 2.0,  64),
    "aggressive": (0.15, 0.80, 4.0, 128),
}
_CACHE_MIN_MIB = 2  # never drop a page cache below the v0.6.1 writer floor


@dataclass(frozen=True)
class SqliteMemPlan:
    """Literal PRAGMA values for a SQLite connection. ``mmap_size`` is bytes;
    the cache sizes use SQLite's negative-KiB convention (-2048 == 2 MB)."""

    mmap_size: int
    writer_cache_size: int
    reader_cache_size: int


def _conservative_plan() -> "SqliteMemPlan":
    return SqliteMemPlan(
        mmap_size=_CONSERVATIVE_MMAP,
        writer_cache_size=_CONSERVATIVE_WRITER_CACHE,
        reader_cache_size=_CONSERVATIVE_READER_CACHE,
    )


def _scaled_plan(budget_bytes: int, n_shards: int, mmap_frac: float,
                 mmap_cap_gib: float, cache_max_mib: int) -> "SqliteMemPlan":
    """Split a total SQLite budget across shards into mmap + page cache.
    Falls back to the conservative plan when the budget cannot fund mmap."""
    if budget_bytes <= 0:
        return _conservative_plan()
    per_shard = budget_bytes / max(1, n_shards)
    mmap = int(min(mmap_cap_gib * _MEM_GiB, mmap_frac * per_shard))
    if mmap <= 0:
        return _conservative_plan()
    cache_bytes = min(cache_max_mib * _MEM_MiB,
                      max(_CACHE_MIN_MIB * _MEM_MiB, (1.0 - mmap_frac) * per_shard))
    cache_kib = int(cache_bytes // 1024)
    return SqliteMemPlan(mmap_size=mmap,
                         writer_cache_size=-cache_kib,
                         reader_cache_size=-cache_kib)


def _resolve_available_bytes(available_bytes: Optional[int]) -> Optional[int]:
    if available_bytes is not None:
        return available_bytes
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        log.warning("psutil.virtual_memory() failed; SQLite mem budget -> "
                    "conservative", exc_info=True)
        return None


def sqlite_memory_budget(n_shards: int, *,
                         available_bytes: Optional[int] = None) -> "SqliteMemPlan":
    """Resolve per-connection SQLite PRAGMA values for the host + shard count.

    Profile via ``HELIX_MEM_PROFILE`` (default ``auto``):
      auto         dynamic budget = (available - 25% reserve) / n_shards
      aggressive   same split, leaner 15% reserve + higher caps
      conservative byte-identical to v0.6.1 (mmap off, 2/4 MB caches)
      <N>gb        pin the TOTAL SQLite budget to N GiB, host-independent

    Hard overrides (win over the profile):
      HELIX_SQLITE_MMAP_SIZE   per-conn mmap_size, in bytes
      HELIX_SQLITE_CACHE_SIZE  raw cache_size pragma value (negative = KiB)
    """
    profile = os.environ.get("HELIX_MEM_PROFILE", "auto").strip().lower()

    if profile == "conservative":
        plan = _conservative_plan()
    elif profile.endswith("gb") and profile[:-2].strip().replace(".", "", 1).isdigit():
        budget = int(float(profile[:-2].strip()) * _MEM_GiB)
        _, mf, cap, cmax = _MEM_PROFILES["auto"]
        plan = _scaled_plan(budget, n_shards, mf, cap, cmax)
    else:
        rf, mf, cap, cmax = _MEM_PROFILES.get(profile, _MEM_PROFILES["auto"])
        avail = _resolve_available_bytes(available_bytes)
        if avail is None:
            plan = _conservative_plan()
        else:
            reserve = max(4 * _MEM_GiB, int(rf * avail))
            plan = _scaled_plan(max(0, avail - reserve), n_shards, mf, cap, cmax)

    # Hard env overrides win over the profile.
    mmap_env = os.environ.get("HELIX_SQLITE_MMAP_SIZE")
    cache_env = os.environ.get("HELIX_SQLITE_CACHE_SIZE")
    if mmap_env or cache_env:
        mmap_size = int(mmap_env) if mmap_env else plan.mmap_size
        if cache_env:
            cs = int(cache_env)
            plan = SqliteMemPlan(mmap_size, cs, cs)
        else:
            plan = SqliteMemPlan(mmap_size, plan.writer_cache_size,
                                 plan.reader_cache_size)
    return plan
