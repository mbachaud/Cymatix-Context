# Hardware Detection — PR1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Centralize torch device detection in a new `helix_context/hardware.py` module, plumb it through all four torch-using backends (deberta/nli/splade/sema), surface device + fallback state via logs / `/health` / tray balloon, and chunk batches in deberta+nli+splade based on a VRAM-keyed table — without changing observable behavior on our 24 GB CUDA rig (verified by bench gate ≤ 5 s p95 delta).

**Architecture:** Single `HardwareInfo` dataclass returned by a cached `get_hardware()` singleton. Auto-mode picker walks CUDA → ROCm → MPS → CPU; explicit-device requests fall back loudly to CPU on probe failure but never block. Backend chunking uses `recommended_batch_size(model)` which consults a `_BATCH_TABLE` keyed on `(device_type, vram_total_or_ram_gb)`. Config plumbed via new `[hardware]` TOML section with `HELIX_DEVICE` env-var override; `[ribosome] device` deprecated for one release with a backwards-compat read.

**Tech Stack:** Python 3.12, `torch>=2.0`, `psutil>=5.9`, **new dep: `py-cpuinfo>=9.0`** (added to `launcher` extra). FastAPI `/health` endpoint, pystray balloon notifications, pytest with `monkeypatch` fixtures.

**Spec:** [`docs/specs/2026-05-04-hardware-detection-design.md`](../specs/2026-05-04-hardware-detection-design.md)

**Branch:** `feat/hardware-detection` (already created from master at `7d96320`)

---

## File structure

### NEW files

| File | Responsibility |
|---|---|
| `helix_context/hardware.py` | `HardwareInfo` dataclass + `get_hardware()` singleton + `_BATCH_TABLE` + `recommended_batch_size()` + `_probe()` + auto-mode picker + multi-GPU selection + env/config resolution |
| `tests/test_hardware.py` | Layer 1 mocked-torch unit tests for everything in `hardware.py` |

### MODIFIED files

| File | Change |
|---|---|
| `pyproject.toml` | Add `py-cpuinfo>=9.0` to the `launcher` extra |
| `helix_context/config.py` | Parse `[hardware]` section; deprecation read for `[ribosome] device` |
| `helix.toml` | Add documented `[hardware]` block |
| `helix_context/deberta_backend.py` | Consult `get_hardware()` in `__init__`; chunk batches in `re_rank()` and `splice()` |
| `helix_context/nli_backend.py` | Consult `get_hardware()` in `__init__`; chunk batches in `classify_batch()` |
| `helix_context/splade_backend.py` | Consult `get_hardware()` in `_ensure_loaded()`; default `batch_size` from `recommended_batch_size("splade")` |
| `helix_context/sema.py` | `SemanticEncoder` consults `get_hardware()` for default device |
| `helix_context/server.py` | Add `hardware` block to `/health` JSON response (around line 2213) |
| `helix_context/launcher/tray.py` | Fallback balloon + sentinel-file dedup (mirrors install-pending pattern) |
| `tests/test_launcher_tray.py` | Add tests for fallback balloon trigger + sentinel dedup |
| `tests/test_server.py` | Add test for `/health` `hardware` block presence |
| `tests/test_config.py` | Add tests for `[hardware]` parsing + `[ribosome] device` deprecation shim |

**Out of scope (PR2):** Wiring up actual ROCm + MPS detection (PR1 leaves these device-type strings parseable but resolves to CPU fallback in practice — the picker's branches are stubbed). GHA CI workflow. Enhancement issue template. Capable-but-unverified disclaimers on alt-device paths.

---

## Task list

There are 14 tasks. Tasks 1–10 build the hardware module and config plumbing; Task 11 plumbs into all four backends; Tasks 12–13 surface state to users; Task 14 is the bench gate.

---

### Task 1: Add `py-cpuinfo` dependency

**Files:**
- Modify: `pyproject.toml`

This unblocks Task 4 (CPU detection). Pure dep change, no tests.

- [ ] **Step 1: Find the `launcher` extras block**

Run: `grep -n "launcher" pyproject.toml`
Expected: locates the `launcher = [...]` and `launcher-tray = [...]` extras lines (sidecar PR landed these around 2026-05-04).

- [ ] **Step 2: Add `py-cpuinfo>=9.0` to both `launcher` and `launcher-tray` extras**

```toml
launcher = ["jinja2>=3.1", "psutil>=5.9", "platformdirs>=4.0", "py-cpuinfo>=9.0"]
launcher-tray = [
    "jinja2>=3.1", "psutil>=5.9", "platformdirs>=4.0", "py-cpuinfo>=9.0",
    "pystray>=0.19", "Pillow>=10",
    "pywin32>=306; sys_platform == 'win32'",
]
```

- [ ] **Step 3: Install the new dep into the active venv**

Run: `pip install py-cpuinfo>=9.0`
Expected: `Successfully installed py-cpuinfo-9.X.X`

Verify: `python -c "import cpuinfo; print(cpuinfo.get_cpu_info()['brand_raw'])"` prints something non-empty (e.g., `AMD Ryzen 9 7900X`).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "deps(launcher): add py-cpuinfo>=9.0 for hardware-detection cpu_brand source"
```

---

### Task 2: `HardwareInfo` dataclass + cached singleton skeleton

**Files:**
- Create: `helix_context/hardware.py`
- Create: `tests/test_hardware.py`

Establishes the public surface. Singleton starts empty; subsequent tasks fill it in.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hardware.py`:

```python
"""Unit tests for helix_context.hardware (mocked-torch).

All tests use monkeypatch to mock torch internals — they never touch a real
GPU. Pattern mirrors tests/test_observability_paths.py.
"""

from __future__ import annotations

import dataclasses
import logging
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hardware.py -v`
Expected: `ImportError: cannot import name 'hardware' from 'helix_context'` or similar — the module doesn't exist yet.

- [ ] **Step 3: Create the minimal hardware module**

Create `helix_context/hardware.py`:

```python
"""Hardware detection + device backend dispatch for helix-context.

Single source of truth for which torch device backends consult. Auto-mode
picker walks CUDA -> ROCm -> MPS -> CPU; explicit-device requests fall back
loudly to CPU on probe failure but never block. See:
  docs/specs/2026-05-04-hardware-detection-design.md

Public surface:
  HardwareInfo            -- frozen dataclass returned by get_hardware()
  get_hardware()          -- cached singleton; first call performs detection
  reset_for_test()        -- test-only cache reset
  recommended_batch_size  -- table-driven, override-aware

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hardware.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/hardware.py tests/test_hardware.py
git commit -m "feat(hardware): HardwareInfo dataclass + cached singleton scaffolding"
```

---

### Task 3: CPU detection (`cpu_arch` + `cpu_brand` + `system_ram_gb`)

**Files:**
- Modify: `helix_context/hardware.py`
- Modify: `tests/test_hardware.py`

Builds the CPU side of detection. `cpu_brand` resolution: py-cpuinfo → platform.processor() → "unknown CPU".

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hardware.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hardware.py::test_cpu_brand_from_py_cpuinfo -v`
Expected: `AttributeError: module 'helix_context.hardware' has no attribute '_detect_cpu'`.

- [ ] **Step 3: Implement `_detect_cpu()` + the cpuinfo wrapper**

In `helix_context/hardware.py`, add near the top (after imports):

```python
import platform
from typing import Any, Dict


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
```

Update `_detect()` to use `_detect_cpu()` for the CPU fields:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hardware.py -v`
Expected: 7 passed (2 from Task 2 + 5 new).

- [ ] **Step 5: Commit**

```bash
git add helix_context/hardware.py tests/test_hardware.py
git commit -m "feat(hardware): CPU detection (cpu_arch + cpu_brand resolution + system_ram_gb)"
```

---

### Task 4: Probe protocol (`_probe()`)

**Files:**
- Modify: `helix_context/hardware.py`
- Modify: `tests/test_hardware.py`

Probe is the "looks present, isn't usable" gate. Will be consumed by the auto-mode picker in Task 5.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hardware.py`:

```python
def test_probe_success(monkeypatch):
    """Round-trip succeeds -> (True, None)."""
    class _FakeTensor:
        def to(self, device): return self
        def cpu(self): return self
    monkeypatch.setattr("torch.zeros", lambda *a, **kw: _FakeTensor())
    ok, reason = hardware._probe("cuda:0")
    assert ok is True
    assert reason is None


def test_probe_failure_returns_reason(monkeypatch):
    """torch.zeros() raises -> (False, formatted reason)."""
    def _raise(*a, **kw):
        raise RuntimeError("CUDA driver version is insufficient")
    monkeypatch.setattr("torch.zeros", _raise)
    ok, reason = hardware._probe("cuda:0")
    assert ok is False
    assert reason is not None
    assert "RuntimeError" in reason
    assert "CUDA driver version is insufficient" in reason


def test_probe_to_failure(monkeypatch):
    """torch.zeros() succeeds but .to(device) raises -> probe fails."""
    class _BadTensor:
        def to(self, device):
            raise RuntimeError("device not available")
        def cpu(self): return self
    monkeypatch.setattr("torch.zeros", lambda *a, **kw: _BadTensor())
    ok, reason = hardware._probe("cuda:0")
    assert ok is False
    assert "device not available" in reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hardware.py::test_probe_success -v`
Expected: `AttributeError: module 'helix_context.hardware' has no attribute '_probe'`.

- [ ] **Step 3: Implement `_probe()`**

Add to `helix_context/hardware.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hardware.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/hardware.py tests/test_hardware.py
git commit -m "feat(hardware): _probe() — 1-element round-trip device usability check"
```

---

### Task 5: Auto-mode picker (CUDA + ROCm + MPS + CPU)

**Files:**
- Modify: `helix_context/hardware.py`
- Modify: `tests/test_hardware.py`

The full auto-mode picking order. Single-GPU-only — multi-GPU fall-through is Task 6.

- [ ] **Step 1: Write the failing tests + the shared `mock_torch` fixture**

Append to `tests/test_hardware.py`:

```python
@pytest.fixture
def mock_torch(monkeypatch):
    """Shared torch mock for picker tests. Returns a state dict so each
    test can configure which backends are advertised. Defaults: CPU-only.
    """
    state = {
        "cuda_available": False,
        "cuda_device_count": 0,
        "cuda_device_names": [],
        "cuda_mem": [],            # list of (free_gb, total_gb) per index
        "hip_version": None,
        "mps_available": False,
        "mps_built": False,
        "probe_results": {},       # device_str -> (ok, reason) overrides
    }

    def _is_available(): return state["cuda_available"]
    def _device_count(): return state["cuda_device_count"]
    def _get_device_name(i): return state["cuda_device_names"][i]
    def _mem_get_info(i):
        free_gb, total_gb = state["cuda_mem"][i]
        return (int(free_gb * 1024**3), int(total_gb * 1024**3))
    def _mps_is_available(): return state["mps_available"]
    def _mps_is_built(): return state["mps_built"]

    monkeypatch.setattr("torch.cuda.is_available", _is_available)
    monkeypatch.setattr("torch.cuda.device_count", _device_count)
    monkeypatch.setattr("torch.cuda.get_device_name", _get_device_name)
    monkeypatch.setattr("torch.cuda.mem_get_info", _mem_get_info)
    monkeypatch.setattr("torch.backends.mps.is_available", _mps_is_available)
    monkeypatch.setattr("torch.backends.mps.is_built", _mps_is_built)

    import torch
    monkeypatch.setattr(torch.version, "hip", state["hip_version"], raising=False)

    def _probe_stub(device_str: str):
        if device_str in state["probe_results"]:
            return state["probe_results"][device_str]
        return (True, None)
    monkeypatch.setattr(hardware, "_probe", _probe_stub)

    def _set_hip(v):
        state["hip_version"] = v
        monkeypatch.setattr(torch.version, "hip", v, raising=False)
    state["set_hip"] = _set_hip
    return state


def test_auto_picks_cuda_when_available(mock_torch):
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["NVIDIA GeForce RTX 4090"]
    mock_torch["cuda_mem"] = [(22.4, 24.0)]
    info = hardware._detect()
    assert info.device_type == "cuda"
    assert info.device == "cuda:0"
    assert info.device_name == "NVIDIA GeForce RTX 4090"
    assert info.vram_total_gb == pytest.approx(24.0, rel=1e-3)


def test_auto_picks_cpu_when_nothing_available(mock_torch):
    info = hardware._detect()
    assert info.device_type == "cpu"
    assert info.device == "cpu"


def test_auto_picks_rocm_when_hip_advertised(mock_torch):
    """ROCm builds set torch.version.hip; cuda.is_available() also returns
    True on a ROCm build (HIP devices surface through the cuda API)."""
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["AMD Radeon RX 7900 XTX"]
    mock_torch["cuda_mem"] = [(20.0, 24.0)]
    mock_torch["set_hip"]("5.7.0")
    info = hardware._detect()
    assert info.device_type == "rocm"
    assert info.device == "rocm:0"
    assert "Radeon" in info.device_name


def test_auto_picks_mps_when_only_mps_available(mock_torch):
    mock_torch["mps_available"] = True
    mock_torch["mps_built"] = True
    info = hardware._detect()
    assert info.device_type == "mps"
    assert info.device == "mps"


def test_auto_falls_through_when_cuda_probe_fails(mock_torch):
    """Mocked-multi: simulate CUDA advertised but probe fails; MPS available.
    Real-world this can't happen (CUDA + MPS aren't on the same wheel) but
    the fall-through logic must work for the explicit-fallback path too."""
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["Broken GPU"]
    mock_torch["cuda_mem"] = [(0.0, 4.0)]
    mock_torch["probe_results"]["cuda:0"] = (False, "RuntimeError: bad")
    mock_torch["mps_available"] = True
    mock_torch["mps_built"] = True
    info = hardware._detect()
    assert info.device_type == "mps"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hardware.py::test_auto_picks_cuda_when_available -v`
Expected: assertion failure (current `_detect` always returns CPU).

- [ ] **Step 3: Implement the picker**

Replace `_detect()` in `helix_context/hardware.py` and add helpers:

```python
def _detect_cuda_or_rocm(rocm: bool) -> Optional[Dict[str, Any]]:
    """Returns dict with device fields, or None if not available.
    Single-device pick (multi-GPU enumeration is Task 6)."""
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() == 0:
        return None
    if rocm and getattr(torch.version, "hip", None) is None:
        return None
    if not rocm and getattr(torch.version, "hip", None) is not None:
        return None
    idx = 0
    free_b, total_b = torch.cuda.mem_get_info(idx)
    device_str = f"{'rocm' if rocm else 'cuda'}:{idx}"
    ok, reason = _probe(f"cuda:{idx}")
    if not ok:
        log.warning("Device %s probe failed: %s", device_str, reason)
        return None
    return {
        "device": device_str,
        "device_type": "rocm" if rocm else "cuda",
        "device_name": torch.cuda.get_device_name(idx),
        "vram_total_gb": total_b / (1024 ** 3),
        "vram_free_gb": free_b / (1024 ** 3),
    }


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
    requested = "auto"  # Task 7 wires env+config

    for label, fn in (
        ("cuda", lambda: _detect_cuda_or_rocm(rocm=False)),
        ("rocm", lambda: _detect_cuda_or_rocm(rocm=True)),
        ("mps",  _detect_mps),
    ):
        try:
            d = fn()
        except Exception:
            log.warning("hardware candidate %s failed", label, exc_info=True)
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
        fallback_reason=None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hardware.py -v`
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/hardware.py tests/test_hardware.py
git commit -m "feat(hardware): auto-mode picker (CUDA -> ROCm -> MPS -> CPU)"
```

---

### Task 6: Multi-GPU device selection

**Files:**
- Modify: `helix_context/hardware.py`
- Modify: `tests/test_hardware.py`

Pick the device with most free VRAM; on per-device probe failure, fall through to next-best. Regression pin for spec-review issue B2.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hardware.py`:

```python
def test_multi_gpu_picks_largest_free_vram(mock_torch):
    """Two healthy GPUs — pick the one with more free VRAM."""
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 2
    mock_torch["cuda_device_names"] = ["RTX 3070", "RTX 4090"]
    mock_torch["cuda_mem"] = [(4.0, 8.0), (22.0, 24.0)]
    info = hardware._detect()
    assert info.device == "cuda:1"
    assert info.device_name == "RTX 4090"


def test_multi_gpu_dead_first_device_falls_through(mock_torch, monkeypatch):
    """device-0 mem_get_info raises (dead); device-1 healthy. Pick cuda:1."""
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 2
    mock_torch["cuda_device_names"] = ["DeadCard", "RTX 4090"]
    mock_torch["cuda_mem"] = [(0.0, 0.0), (22.0, 24.0)]

    def _mem(i):
        if i == 0:
            raise RuntimeError("device 0 is dead")
        free_gb, total_gb = mock_torch["cuda_mem"][i]
        return (int(free_gb * 1024**3), int(total_gb * 1024**3))
    monkeypatch.setattr("torch.cuda.mem_get_info", _mem)

    info = hardware._detect()
    assert info.device == "cuda:1"
    assert info.device_name == "RTX 4090"


def test_multi_gpu_probe_failure_on_best_falls_through(mock_torch):
    """Best-VRAM device probe fails; pick the next-best healthy one."""
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 2
    mock_torch["cuda_device_names"] = ["RTX 4090 (broken)", "RTX 3070"]
    mock_torch["cuda_mem"] = [(22.0, 24.0), (6.0, 8.0)]
    mock_torch["probe_results"]["cuda:0"] = (False, "RuntimeError: kernel launch failed")
    info = hardware._detect()
    assert info.device == "cuda:1"
    assert info.device_name == "RTX 3070"


def test_multi_gpu_all_dead_falls_through_to_cpu(mock_torch):
    """All CUDA devices fail their probes -> CUDA candidate rejected,
    auto-mode falls through to CPU (no MPS in this scenario)."""
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 2
    mock_torch["cuda_device_names"] = ["broken1", "broken2"]
    mock_torch["cuda_mem"] = [(4.0, 8.0), (4.0, 8.0)]
    mock_torch["probe_results"]["cuda:0"] = (False, "bad")
    mock_torch["probe_results"]["cuda:1"] = (False, "bad")
    info = hardware._detect()
    assert info.device_type == "cpu"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hardware.py::test_multi_gpu_picks_largest_free_vram -v`
Expected: assertion failure (current single-device picker hardcodes idx=0).

- [ ] **Step 3: Replace single-device pick with enumerate-pick-probe-fallthrough**

In `helix_context/hardware.py`, replace `_detect_cuda_or_rocm`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hardware.py -v`
Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/hardware.py tests/test_hardware.py
git commit -m "feat(hardware): multi-GPU pick by free VRAM with probe-fall-through

Regression pin for spec-review B2 — dead device-0 + healthy device-1
must select cuda:1, not reject CUDA outright."
```

---

### Task 7: Explicit-device requested + `HELIX_DEVICE` env override + fallback policy

**Files:**
- Modify: `helix_context/hardware.py`
- Modify: `tests/test_hardware.py`

Wires `requested_device` from env + (later: config) into `_detect()`. Explicit-device mismatch falls back **directly to CPU** (skips ROCm/MPS — see spec §5.4).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hardware.py`:

```python
def test_explicit_cuda_succeeds(mock_torch, monkeypatch):
    monkeypatch.setenv("HELIX_DEVICE", "cuda")
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["RTX 4090"]
    mock_torch["cuda_mem"] = [(22.0, 24.0)]
    info = hardware._detect()
    assert info.device_type == "cuda"
    assert info.requested_device == "cuda"
    assert info.fallback_reason is None


def test_explicit_cuda_falls_back_to_cpu_on_probe_failure(mock_torch, monkeypatch):
    """Explicit cuda + no GPU available -> cpu directly (NOT rocm/mps even if available).

    Spec §5.4 asymmetry: auto picks the best available; explicit means
    'I want this exactly, downgrade to CPU if not there'."""
    monkeypatch.setenv("HELIX_DEVICE", "cuda")
    mock_torch["cuda_available"] = False
    mock_torch["mps_available"] = True
    mock_torch["mps_built"] = True
    info = hardware._detect()
    assert info.device_type == "cpu"
    assert info.fallback_reason is not None
    assert "cuda" in info.fallback_reason.lower()


def test_helix_device_env_var_case_insensitive(mock_torch, monkeypatch):
    monkeypatch.setenv("HELIX_DEVICE", "CPU")
    info = hardware._detect()
    assert info.device_type == "cpu"
    assert info.requested_device == "cpu"


def test_helix_device_env_var_invalid_falls_back_to_auto(mock_torch, monkeypatch, caplog):
    monkeypatch.setenv("HELIX_DEVICE", "nonsense")
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["RTX 4090"]
    mock_torch["cuda_mem"] = [(22.0, 24.0)]
    with caplog.at_level(logging.WARNING, logger="helix.hardware"):
        info = hardware._detect()
    assert info.requested_device == "auto"
    assert info.device_type == "cuda"
    assert any(
        "invalid helix_device" in rec.message.lower()
        or "ignoring helix_device" in rec.message.lower()
        for rec in caplog.records
    )


def test_explicit_mps_on_non_mps_host_falls_back_to_cpu(mock_torch, monkeypatch):
    monkeypatch.setenv("HELIX_DEVICE", "mps")
    info = hardware._detect()
    assert info.device_type == "cpu"
    assert info.fallback_reason is not None
    assert "mps" in info.fallback_reason.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hardware.py::test_explicit_cuda_succeeds -v`
Expected: assertion failure (`requested_device == "auto"` regardless of env var; env not yet read).

- [ ] **Step 3: Add env-var resolution + explicit-device path**

In `helix_context/hardware.py`, add near the top:

```python
import os

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
```

Update `_detect()` to consult `_resolve_requested_device()` and dispatch:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hardware.py -v`
Expected: 24 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/hardware.py tests/test_hardware.py
git commit -m "feat(hardware): HELIX_DEVICE env override + explicit-device fallback to CPU

Implements spec §5.4 — explicit device request that fails probes falls
back to CPU directly (skips other GPU candidates). Auto-mode unchanged."
```

---

### Task 8: Batch-size table + `recommended_batch_size` + `batch_size_overrides`

**Files:**
- Modify: `helix_context/hardware.py`
- Modify: `tests/test_hardware.py`

Implements `recommended_batch_size(model)` consulting `batch_size_overrides` first, then the `_BATCH_TABLE`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hardware.py`:

```python
def test_batch_size_24gb_cuda_tier(mock_torch):
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["RTX 4090"]
    mock_torch["cuda_mem"] = [(22.0, 24.0)]
    hardware.reset_for_test()
    assert hardware.recommended_batch_size("rerank") == 64
    assert hardware.recommended_batch_size("splice") == 128
    assert hardware.recommended_batch_size("splade") == 32
    assert hardware.recommended_batch_size("nli") == 32


def test_batch_size_4gb_cuda_tier(mock_torch):
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["GTX 1650"]
    mock_torch["cuda_mem"] = [(3.5, 4.0)]
    hardware.reset_for_test()
    assert hardware.recommended_batch_size("rerank") == 8


def test_batch_size_under_4gb_cuda_tier(mock_torch):
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["MX150"]
    mock_torch["cuda_mem"] = [(1.5, 2.0)]
    hardware.reset_for_test()
    assert hardware.recommended_batch_size("rerank") == 4


def test_batch_size_cpu_tier_uses_system_ram(monkeypatch, mock_torch):
    """CPU batch sizes key on system_ram_gb, not VRAM."""
    class _FakeVM:
        total = 16 * 1024 ** 3  # 16 GiB
    monkeypatch.setattr("psutil.virtual_memory", lambda: _FakeVM())
    hardware.reset_for_test()
    assert hardware.recommended_batch_size("rerank") == 8


def test_batch_size_total_not_free_drives_lookup(mock_torch):
    """Even if free VRAM is tiny, total VRAM picks the tier.

    Regression pin for spec-review B3 — vram_free_gb is informational
    only; the table keys on vram_total_gb.
    """
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["RTX 4090"]
    mock_torch["cuda_mem"] = [(0.5, 24.0)]  # almost all VRAM in use
    hardware.reset_for_test()
    assert hardware.recommended_batch_size("rerank") == 64  # 24GB tier wins


def test_batch_size_override_beats_table(mock_torch):
    """batch_size_overrides field short-circuits the table lookup."""
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["RTX 4090"]
    mock_torch["cuda_mem"] = [(22.0, 24.0)]
    hardware.reset_for_test()
    info = hardware.get_hardware()
    forced = dataclasses.replace(info, batch_size_overrides={"rerank": 16})
    hardware._cached_info = forced  # direct cache poke for test
    assert hardware.recommended_batch_size("rerank") == 16
    assert hardware.recommended_batch_size("splice") == 128


def test_batch_size_unknown_model_returns_minimum(mock_torch):
    """Asking for a model not in the table returns the conservative floor."""
    hardware.reset_for_test()
    assert hardware.recommended_batch_size("unknown_model") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hardware.py::test_batch_size_24gb_cuda_tier -v`
Expected: `AttributeError: module 'helix_context.hardware' has no attribute 'recommended_batch_size'`.

- [ ] **Step 3: Add `_BATCH_TABLE` + `recommended_batch_size`**

In `helix_context/hardware.py`, add near the bottom (after `_detect`):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hardware.py -v`
Expected: 31 passed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/hardware.py tests/test_hardware.py
git commit -m "feat(hardware): batch-size table + recommended_batch_size with overrides

Regression pin for spec-review B3 — table keyed on vram_total_gb (not
free), pinned in test_batch_size_total_not_free_drives_lookup."
```

---

### Task 9: Config parsing for `[hardware]` + `[ribosome] device` deprecation

**Files:**
- Modify: `helix_context/config.py`
- Modify: `helix_context/hardware.py` (consume parsed config)
- Modify: `tests/test_config.py` (add deprecation tests)
- Modify: `tests/test_hardware.py` (add config-flow tests)

Wires the `[hardware]` TOML section through `Config` and into the hardware singleton, with the ribosome.device backwards-compat shim.

- [ ] **Step 1: Inspect current config shape**

Run: `grep -n "device\|class Ribosome\|class Server\|class.*Config" helix_context/config.py | head -30`
Expected: locates `Ribosome` config class + the `from_dict` constructors.

- [ ] **Step 2: Write the failing tests in `tests/test_config.py`**

Append:

```python
def test_hardware_section_parses(tmp_path):
    """[hardware] section parses with all defaults."""
    cfg_text = """
[hardware]
device = "cuda"
batch_sizes = "auto"
low_vram_threshold_gb = 4.0
"""
    p = tmp_path / "helix.toml"
    p.write_text(cfg_text)
    cfg = Config.from_file(str(p))
    assert cfg.hardware.device == "cuda"
    assert cfg.hardware.batch_sizes == {}  # "auto" -> empty override dict
    assert cfg.hardware.low_vram_threshold_gb == 4.0


def test_hardware_section_batch_sizes_dict(tmp_path):
    cfg_text = """
[hardware]
device = "auto"
batch_sizes = { rerank = 16, splice = 32 }
"""
    p = tmp_path / "helix.toml"
    p.write_text(cfg_text)
    cfg = Config.from_file(str(p))
    assert cfg.hardware.batch_sizes == {"rerank": 16, "splice": 32}


def test_ribosome_device_deprecation_warning(tmp_path, caplog):
    """[ribosome] device alone (no [hardware]) triggers deprecation warning."""
    cfg_text = """
[ribosome]
device = "cuda"
"""
    p = tmp_path / "helix.toml"
    p.write_text(cfg_text)
    with caplog.at_level("WARNING", logger="helix.config"):
        cfg = Config.from_file(str(p))
    assert cfg.hardware.device == "cuda"  # ribosome value used
    assert any(
        "ribosome" in rec.message.lower() and "deprecated" in rec.message.lower()
        for rec in caplog.records
    )


def test_hardware_overrides_ribosome_device(tmp_path, caplog):
    """When both are set, [hardware] wins; warning still fires noting override."""
    cfg_text = """
[ribosome]
device = "cpu"

[hardware]
device = "cuda"
"""
    p = tmp_path / "helix.toml"
    p.write_text(cfg_text)
    with caplog.at_level("WARNING", logger="helix.config"):
        cfg = Config.from_file(str(p))
    assert cfg.hardware.device == "cuda"  # [hardware] wins
    assert any(
        "deprecated" in rec.message.lower() and "override" in rec.message.lower()
        for rec in caplog.records
    )


def test_no_device_config_defaults_to_auto(tmp_path):
    """Empty config -> [hardware].device = "auto"."""
    p = tmp_path / "helix.toml"
    p.write_text("# empty\n")
    cfg = Config.from_file(str(p))
    assert cfg.hardware.device == "auto"
    assert cfg.hardware.batch_sizes == {}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_config.py::test_hardware_section_parses -v`
Expected: `AttributeError: 'Config' object has no attribute 'hardware'`.

- [ ] **Step 4: Add `Hardware` config dataclass + parser**

In `helix_context/config.py`, add a `Hardware` dataclass (near `Ribosome`):

```python
@dataclass
class Hardware:
    """[hardware] config — see docs/specs/2026-05-04-hardware-detection-design.md."""
    device: str = "auto"        # auto | cuda | rocm | mps | cpu
    batch_sizes: Dict[str, int] = field(default_factory=dict)
    low_vram_threshold_gb: float = 4.0
```

In `Config.from_dict()` (or wherever the section parsers live), add:

```python
hw = data.get("hardware", {})
if isinstance(hw.get("batch_sizes"), str) and hw["batch_sizes"] == "auto":
    bs = {}
elif isinstance(hw.get("batch_sizes"), dict):
    bs = {k: int(v) for k, v in hw["batch_sizes"].items()}
else:
    bs = {}
hardware_device = hw.get("device", "auto")

# Deprecation shim for [ribosome] device.
ribosome_device = data.get("ribosome", {}).get("device")
if ribosome_device is not None:
    if "device" in hw:
        log.warning(
            "[ribosome] device is deprecated and was overridden by "
            "[hardware] device=%r. Remove [ribosome] device.", hardware_device,
        )
    else:
        log.warning(
            "[ribosome] device is deprecated; move to [hardware] device. "
            "Using ribosome.device=%r for now.", ribosome_device,
        )
        hardware_device = ribosome_device

hardware_cfg = Hardware(
    device=str(hardware_device),
    batch_sizes=bs,
    low_vram_threshold_gb=float(hw.get("low_vram_threshold_gb", 4.0)),
)
cfg.hardware = hardware_cfg
```

(Implementer: place these blocks in the existing `from_dict`/`from_file` flow at the right indent level. The exact line is whatever follows the existing `[ribosome]` parse.)

- [ ] **Step 5: Run config tests**

Run: `pytest tests/test_config.py -k "hardware or ribosome_device" -v`
Expected: 5 passed.

- [ ] **Step 6: Wire config to `hardware.get_hardware()` via `init_from_config`**

In `helix_context/hardware.py`, modify `_resolve_requested_device()` to also accept a config-supplied default:

```python
def _resolve_requested_device(config_device: str = "auto") -> str:
    """Resolve requested device. Order: HELIX_DEVICE env > config > 'auto'."""
    env_value = os.environ.get("HELIX_DEVICE")
    if env_value is not None:
        normalized = env_value.strip().lower()
        if normalized in _VALID_DEVICES:
            return normalized
        log.warning(
            "Invalid HELIX_DEVICE=%r (valid: %s); ignoring HELIX_DEVICE",
            env_value, ", ".join(_VALID_DEVICES),
        )
    if config_device.lower() in _VALID_DEVICES:
        return config_device.lower()
    log.warning("Invalid [hardware] device=%r; using 'auto'", config_device)
    return "auto"
```

Add a public init function:

```python
def init_from_config(config_device: str = "auto",
                     batch_size_overrides: Optional[Mapping[str, int]] = None) -> HardwareInfo:
    """One-shot init at server startup. Calls _detect with config-driven
    requested_device and stamps batch_size_overrides onto the singleton.

    Idempotent: returns the cached HardwareInfo on subsequent calls."""
    global _cached_info, _config_device, _config_overrides
    if _cached_info is not None:
        return _cached_info
    _config_device = config_device
    _config_overrides = dict(batch_size_overrides) if batch_size_overrides else {}
    _cached_info = _detect()
    return _cached_info
```

Add module-level state for these defaults near `_cached_info`:

```python
_config_device: str = "auto"
_config_overrides: Mapping[str, int] = {}
```

Update `_detect()` to consume them:

```python
def _detect() -> HardwareInfo:
    cpu = _detect_cpu()
    requested = _resolve_requested_device(_config_device)
    overrides = dict(_config_overrides)
    # ...rest unchanged, except add `batch_size_overrides=overrides` to
    # ALL HardwareInfo(...) constructions in this function.
```

Make sure both the success-path and CPU-fallback HardwareInfo constructors include `batch_size_overrides=overrides`.

Update `reset_for_test()` to also reset the config state:

```python
def reset_for_test() -> None:
    global _cached_info, _config_device, _config_overrides
    _cached_info = None
    _config_device = "auto"
    _config_overrides = {}
```

- [ ] **Step 7: Add hardware-side test for config flow**

Append to `tests/test_hardware.py`:

```python
def test_init_from_config_routes_through_singleton(mock_torch):
    """init_from_config sets requested_device + batch_size_overrides."""
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["RTX 4090"]
    mock_torch["cuda_mem"] = [(22.0, 24.0)]
    hardware.reset_for_test()
    info = hardware.init_from_config(
        config_device="cuda",
        batch_size_overrides={"rerank": 8},
    )
    assert info.requested_device == "cuda"
    assert info.batch_size_overrides == {"rerank": 8}
    assert hardware.recommended_batch_size("rerank") == 8  # override wins
```

Run: `pytest tests/test_hardware.py -v`
Expected: 32 passed.

- [ ] **Step 8: Commit**

```bash
git add helix_context/hardware.py helix_context/config.py tests/test_config.py tests/test_hardware.py
git commit -m "feat(hardware): config plumbing — [hardware] section + ribosome.device shim"
```

---

### Task 10: Document `[hardware]` block in `helix.toml`

**Files:**
- Modify: `helix.toml`

Pure config-doc change. Default values match the dataclass defaults.

- [ ] **Step 1: Add the documented `[hardware]` block**

Insert near the top of `helix.toml` (above `[budget]` is fine; alphabetical order across sections is not enforced):

```toml
[hardware]
# Device picker. "auto" picks best-available (cuda -> rocm -> mps -> cpu).
# Explicit values fall back loudly to CPU on probe failure; helix never
# blocks on hardware mismatch -- see /health for fallback state.
# Override one-shot via HELIX_DEVICE=cpu env var.
device = "auto"        # auto | cuda | rocm | mps | cpu

# Batch-size policy. "auto" consults the VRAM/RAM-aware table in
# helix_context/hardware.py. Override per model when tuning:
#   batch_sizes = { rerank = 16, splice = 32, splade = 8, nli = 8 }
batch_sizes = "auto"

# Soft-warn threshold. Below this, /health returns a "low_vram" hint and
# the tray surfaces a one-time balloon. Set to 0 to disable.
low_vram_threshold_gb = 4.0
```

- [ ] **Step 2: Verify config still loads**

Run: `python -c "from helix_context.config import Config; c = Config.from_file('helix.toml'); print(c.hardware)"`
Expected: prints `Hardware(device='auto', batch_sizes={}, low_vram_threshold_gb=4.0)`.

- [ ] **Step 3: Commit**

```bash
git add helix.toml
git commit -m "docs(helix.toml): add [hardware] block with documented defaults"
```

---

### Task 11: Plumb `get_hardware()` through all four backends

This task is split into 4 sub-tasks (one per backend) so each commit is reviewable independently.

#### Task 11a: `deberta_backend.py` — consult hardware + chunk `re_rank` and `splice`

**Files:**
- Modify: `helix_context/deberta_backend.py`
- Modify: `tests/test_ribosome.py`

- [ ] **Step 1: Read the current `re_rank` and `splice` to understand the all-at-once tokenize pattern**

Run: `grep -n "def re_rank\|def splice\|tokenizer(.*texts_a" helix_context/deberta_backend.py`
Expected: locates the entry points + the tokenizer call sites.

- [ ] **Step 2: Write a test that PINS the chunked-batch behavior**

In `tests/test_ribosome.py`, add a test that mocks the tokenizer + model, forces a 16-batch via `recommended_batch_size`, feeds 100 candidates, and asserts the tokenizer was called 7 times (6 full + 1 partial). Adapt to existing fixtures in that file.

- [ ] **Step 3: Run test to verify it fails**

Expected: the test fails because `re_rank` currently tokenizes all candidates in one call.

- [ ] **Step 4: Refactor `re_rank` to chunk**

In `helix_context/deberta_backend.py`, change `re_rank` from the all-at-once tokenize to chunked iteration:

```python
def re_rank(self, query, candidates, k=5):
    if not candidates:
        return []
    if len(candidates) <= k:
        return candidates

    from helix_context.hardware import recommended_batch_size
    batch_size = recommended_batch_size("rerank")

    texts_a, texts_b = [], []
    for g in candidates:
        # ... (same as before — build the pair lists)
        pass

    all_scores = []
    for i in range(0, len(texts_a), batch_size):
        chunk_a = texts_a[i : i + batch_size]
        chunk_b = texts_b[i : i + batch_size]
        encodings = self._rerank_tokenizer(
            chunk_a, chunk_b,
            truncation=True, max_length=256, padding=True,
            return_tensors="pt",
        ).to(self._device)
        with torch.no_grad():
            outputs = self._rerank_model(**encodings)
        scores = outputs.logits.squeeze(-1)
        scores = torch.clamp(scores, 0.0, 1.0).cpu().tolist()
        if isinstance(scores, float):
            scores = [scores]
        all_scores.extend(scores)

    # ... (rest of function unchanged: zip with candidates, sort, take top-k)
```

Apply the same chunking to `splice()`. The pair-building and post-processing stay the same; only the tokenize+forward block is wrapped in the `for i in range(0, ..., batch_size)` loop and per-chunk results are concatenated.

Update `__init__` to default the device argument to `None` and consult `get_hardware()` when None:

```python
def __init__(self, ..., device: Optional[str] = None, ...):
    if device is None:
        from helix_context.hardware import get_hardware
        device = get_hardware().device
    self._device = torch.device(device)
    # ... rest unchanged
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_ribosome.py -v`
Expected: all existing tests still pass + the new chunked-batch test passes.

- [ ] **Step 6: Commit**

```bash
git add helix_context/deberta_backend.py tests/test_ribosome.py
git commit -m "feat(deberta): chunk re_rank+splice batches via recommended_batch_size"
```

#### Task 11b: `nli_backend.py` — consult hardware + chunk `classify_batch`

**Files:**
- Modify: `helix_context/nli_backend.py`
- Modify: `tests/test_ribosome.py`

Same pattern as 11a.

- [ ] **Step 1: Apply the chunking pattern to `classify_batch`**

```python
def classify_batch(self, pairs):
    if not pairs:
        return []
    from helix_context.hardware import recommended_batch_size
    batch_size = recommended_batch_size("nli")
    texts_a = [p[0] for p in pairs]
    texts_b = [p[1] for p in pairs]
    all_results = []
    for i in range(0, len(texts_a), batch_size):
        chunk_a = texts_a[i : i + batch_size]
        chunk_b = texts_b[i : i + batch_size]
        encodings = self._tokenizer(chunk_a, chunk_b, ...).to(self._device)
        with torch.no_grad():
            outputs = self._model(**encodings)
        # ... (same softmax/argmax processing as before, append to all_results)
    return all_results
```

Update `__init__` device default:

```python
def __init__(self, model_path="training/models/nli", device=None):
    if device is None or device == "auto":
        from helix_context.hardware import get_hardware
        device = get_hardware().device
    self._device = torch.device(device)
    # ... rest unchanged
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_ribosome.py -v`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add helix_context/nli_backend.py tests/test_ribosome.py
git commit -m "feat(nli): chunk classify_batch via recommended_batch_size('nli')"
```

#### Task 11c: `splade_backend.py` — consult hardware in `_ensure_loaded` + default `batch_size`

**Files:**
- Modify: `helix_context/splade_backend.py`

`splade_backend.encode_batch()` already chunks (line 116); only the default `batch_size: int = 16` parameter needs to consult the hardware module.

- [ ] **Step 1: Update `_ensure_loaded` to consult hardware module**

```python
def _ensure_loaded(model_name="naver/splade-cocondenser-ensembledistil"):
    global _model, _tokenizer, _device
    if _model is not None:
        return
    import torch
    from transformers import AutoModelForMaskedLM, AutoTokenizer
    from helix_context.hardware import get_hardware

    _device = torch.device(get_hardware().device)
    _tokenizer = AutoTokenizer.from_pretrained(model_name)
    _model = AutoModelForMaskedLM.from_pretrained(model_name).to(_device)
    _model.eval()
    log.info("SPLADE model loaded: %s on %s", model_name, _device)
```

- [ ] **Step 2: Make `encode_batch` consult `recommended_batch_size` for the default**

```python
def encode_batch(texts, top_k=128, batch_size=None,
                 model_name="naver/splade-cocondenser-ensembledistil"):
    import torch
    _ensure_loaded(model_name)
    if batch_size is None:
        from helix_context.hardware import recommended_batch_size
        batch_size = recommended_batch_size("splade")
    # ... rest unchanged
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_ribosome.py tests/test_hardware.py -v`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add helix_context/splade_backend.py
git commit -m "feat(splade): consult hardware module for device + default batch_size"
```

#### Task 11d: `sema.py` — `SemanticEncoder` consults hardware module for default device

**Files:**
- Modify: `helix_context/sema.py`
- Modify: `tests/test_sema.py`

- [ ] **Step 1: Update `SemanticEncoder.__init__`**

```python
def __init__(self, model_name="...", device=None):
    if device is None or device == "auto":
        from helix_context.hardware import get_hardware
        device = get_hardware().device
    from sentence_transformers import SentenceTransformer
    self._model = SentenceTransformer(model_name, device=device)
    # ... rest unchanged
```

- [ ] **Step 2: Add a test pinning the default**

In `tests/test_sema.py`:

```python
def test_semantic_encoder_default_device_from_hardware(monkeypatch):
    from helix_context import hardware
    hardware.reset_for_test()
    monkeypatch.setattr(hardware, "_detect", lambda: hardware.HardwareInfo(
        device="cpu", device_type="cpu", device_name="test",
        vram_total_gb=None, vram_free_gb=None,
        cpu_arch="x86_64", cpu_brand="test",
        system_ram_gb=16.0, requested_device="auto",
        fallback_reason=None, batch_size_overrides={},
    ))
    captured = {}
    class _FakeST:
        def __init__(self, model_name, device):
            captured["device"] = device
        def get_sentence_embedding_dimension(self): return 384
        def encode(self, *a, **kw): return [[0.0] * 384]
    monkeypatch.setattr("sentence_transformers.SentenceTransformer", _FakeST)

    from helix_context.sema import SemanticEncoder
    SemanticEncoder()  # no device arg — should default from hardware
    assert captured["device"] == "cpu"
```

- [ ] **Step 3: Run**

Run: `pytest tests/test_sema.py -v`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add helix_context/sema.py tests/test_sema.py
git commit -m "feat(sema): SemanticEncoder default device from hardware module"
```

---

### Task 12: `/health` endpoint hardware block

**Files:**
- Modify: `helix_context/server.py` (around line 2213)
- Modify: `tests/test_server.py`

Surface fallback state via the existing `/health` JSON response.

- [ ] **Step 1: Find the existing `/health` JSON response building**

Run: `grep -n "health_endpoint\|return.*ribosome_model\|return.*ok" helix_context/server.py | head -10`
Expected: locates line 2213 (`async def health_endpoint`) and the response-building block around lines 2236-2280.

- [ ] **Step 2: Write the failing test in `tests/test_server.py`**

```python
def test_health_endpoint_includes_hardware_block(monkeypatch):
    from helix_context import hardware
    hardware.reset_for_test()
    fake = hardware.HardwareInfo(
        device="cpu", device_type="cpu", device_name="AMD Ryzen 9 7900X",
        vram_total_gb=None, vram_free_gb=None,
        cpu_arch="x86_64", cpu_brand="AMD Ryzen 9 7900X",
        system_ram_gb=64.0, requested_device="cuda",
        fallback_reason="cuda probe failed: RuntimeError: no driver",
        batch_size_overrides={},
    )
    monkeypatch.setattr(hardware, "_detect", lambda: fake)

    response = test_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert "hardware" in body
    hw = body["hardware"]
    assert hw["device"] == "cpu"
    assert hw["device_name"] == "AMD Ryzen 9 7900X"
    assert hw["requested_device"] == "cuda"
    assert hw["fallback_active"] is True
    assert "cuda probe failed" in hw["fallback_reason"]
    assert hw["vram_total_gb"] is None
    assert hw["system_ram_gb"] == 64.0
    assert hw["low_vram_warning"] is False
```

(Implementer: adapt to the existing `test_server.py` fixture pattern — `client.get` or whatever the existing tests use.)

- [ ] **Step 3: Run test to verify failure**

Run: `pytest tests/test_server.py::test_health_endpoint_includes_hardware_block -v`
Expected: `KeyError: 'hardware'` or `AssertionError`.

- [ ] **Step 4: Add `hardware` block to `/health`**

In `helix_context/server.py` `health_endpoint`, just before the final `return` of the response dict, add:

```python
from helix_context.hardware import get_hardware
hw_info = get_hardware()
low_vram = bool(
    hw_info.vram_total_gb is not None
    and hw_info.vram_total_gb < config.hardware.low_vram_threshold_gb
)
hardware_block = {
    "device": hw_info.device,
    "device_name": hw_info.device_name,
    "requested_device": hw_info.requested_device,
    "fallback_active": hw_info.fallback_reason is not None,
    "fallback_reason": hw_info.fallback_reason,
    "vram_total_gb": hw_info.vram_total_gb,
    "system_ram_gb": hw_info.system_ram_gb,
    "low_vram_warning": low_vram,
}
```

Then add `"hardware": hardware_block` to the response dict that's returned.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_server.py -v -k health`
Expected: all health tests green.

- [ ] **Step 6: Commit**

```bash
git add helix_context/server.py tests/test_server.py
git commit -m "feat(server): /health hardware block — surface device + fallback state"
```

---

### Task 13: Tray fallback balloon + sentinel-file dedup

**Files:**
- Modify: `helix_context/launcher/tray.py`
- Modify: `tests/test_launcher_tray.py`

Mirror the install-pending balloon from native-observability sidecar (already in tray.py). Sentinel at `<state_dir>/.hardware-fallback-acknowledged-{requested}-{active}` dedupes per-state-change.

- [ ] **Step 1: Find the install-pending balloon pattern in `tray.py`**

Run: `grep -n "balloon\|notify\|install.*pending\|sentinel" helix_context/launcher/tray.py | head -20`
Expected: locates the install-pending balloon code (sidecar PR) — that's the template.

- [ ] **Step 2: Write the failing tests in `tests/test_launcher_tray.py`**

```python
def test_hardware_fallback_balloon_fires_first_launch(tmp_path, monkeypatch):
    """Balloon fires once when fallback_active=True and no sentinel exists."""
    from helix_context import hardware

    monkeypatch.setattr("helix_context.launcher.observability_paths.state_dir",
                        lambda create=False: tmp_path)
    hardware.reset_for_test()
    fake = hardware.HardwareInfo(
        device="cpu", device_type="cpu", device_name="CPU",
        vram_total_gb=None, vram_free_gb=None,
        cpu_arch="x86_64", cpu_brand="CPU",
        system_ram_gb=16.0, requested_device="cuda",
        fallback_reason="cuda not available",
        batch_size_overrides={},
    )
    monkeypatch.setattr(hardware, "_detect", lambda: fake)

    from helix_context.launcher.tray import _should_fire_hardware_fallback_balloon
    assert _should_fire_hardware_fallback_balloon() is True


def test_hardware_fallback_balloon_dedups_via_sentinel(tmp_path, monkeypatch):
    """Sentinel exists -> balloon suppressed."""
    monkeypatch.setattr("helix_context.launcher.observability_paths.state_dir",
                        lambda create=False: tmp_path)
    sentinel = tmp_path / ".hardware-fallback-acknowledged-cuda-cpu"
    sentinel.touch()

    from helix_context import hardware
    hardware.reset_for_test()
    monkeypatch.setattr(hardware, "_detect", lambda: hardware.HardwareInfo(
        device="cpu", device_type="cpu", device_name="CPU",
        vram_total_gb=None, vram_free_gb=None,
        cpu_arch="x86_64", cpu_brand="CPU", system_ram_gb=16.0,
        requested_device="cuda", fallback_reason="cuda not available",
        batch_size_overrides={},
    ))

    from helix_context.launcher.tray import _should_fire_hardware_fallback_balloon
    assert _should_fire_hardware_fallback_balloon() is False


def test_hardware_fallback_balloon_refires_on_different_state(tmp_path, monkeypatch):
    """Different requested/active combo => different sentinel => balloon fires."""
    monkeypatch.setattr("helix_context.launcher.observability_paths.state_dir",
                        lambda create=False: tmp_path)
    (tmp_path / ".hardware-fallback-acknowledged-cuda-cpu").touch()

    from helix_context import hardware
    hardware.reset_for_test()
    monkeypatch.setattr(hardware, "_detect", lambda: hardware.HardwareInfo(
        device="cpu", device_type="cpu", device_name="CPU",
        vram_total_gb=None, vram_free_gb=None,
        cpu_arch="x86_64", cpu_brand="CPU", system_ram_gb=16.0,
        requested_device="mps", fallback_reason="mps not available",
        batch_size_overrides={},
    ))

    from helix_context.launcher.tray import _should_fire_hardware_fallback_balloon
    # Sentinel is for cuda->cpu; current state is mps->cpu.
    assert _should_fire_hardware_fallback_balloon() is True


def test_hardware_fallback_balloon_skipped_when_no_fallback(tmp_path, monkeypatch):
    """fallback_reason is None => no balloon ever."""
    monkeypatch.setattr("helix_context.launcher.observability_paths.state_dir",
                        lambda create=False: tmp_path)
    from helix_context import hardware
    hardware.reset_for_test()
    monkeypatch.setattr(hardware, "_detect", lambda: hardware.HardwareInfo(
        device="cuda:0", device_type="cuda", device_name="RTX 4090",
        vram_total_gb=24.0, vram_free_gb=22.0,
        cpu_arch="x86_64", cpu_brand="CPU", system_ram_gb=64.0,
        requested_device="auto", fallback_reason=None,
        batch_size_overrides={},
    ))

    from helix_context.launcher.tray import _should_fire_hardware_fallback_balloon
    assert _should_fire_hardware_fallback_balloon() is False
```

- [ ] **Step 3: Run tests to verify failure**

Run: `pytest tests/test_launcher_tray.py -k hardware_fallback -v`
Expected: `ImportError: cannot import name '_should_fire_hardware_fallback_balloon'`.

- [ ] **Step 4: Implement the helper + balloon trigger in `tray.py`**

In `helix_context/launcher/tray.py`, add:

```python
def _hardware_fallback_sentinel_path(requested: str, active: str) -> Path:
    """Sentinel filename encodes the (requested, active) tuple so a
    state-change re-fires the balloon."""
    from helix_context.launcher.observability_paths import state_dir
    return state_dir(create=True) / f".hardware-fallback-acknowledged-{requested}-{active}"


def _should_fire_hardware_fallback_balloon() -> bool:
    """True iff there is an active fallback AND the sentinel for the
    current (requested, active) tuple does not yet exist."""
    from helix_context.hardware import get_hardware
    info = get_hardware()
    if info.fallback_reason is None:
        return False
    sentinel = _hardware_fallback_sentinel_path(info.requested_device, info.device_type)
    return not sentinel.exists()


def _fire_hardware_fallback_balloon(tray_icon) -> None:
    """Fire a one-shot balloon describing the fallback. Caller should
    write the sentinel after firing (so re-launches don't nag)."""
    from helix_context.hardware import get_hardware
    info = get_hardware()
    if info.fallback_reason is None:
        return
    title = "Helix: device fallback active"
    msg = (
        f"Requested {info.requested_device!r}, using {info.device_type!r}. "
        f"Reason: {info.fallback_reason}"
    )
    try:
        tray_icon.notify(msg, title)
    except Exception:
        log.warning("Tray balloon failed; fallback only logged", exc_info=True)
        return
    sentinel = _hardware_fallback_sentinel_path(info.requested_device, info.device_type)
    try:
        sentinel.touch()
    except Exception:
        log.warning("Could not write hardware-fallback sentinel %s", sentinel, exc_info=True)
```

Then in the tray's startup flow (find the existing call site that fires the install-pending balloon for native-observability — that's the template), add a parallel call:

```python
if _should_fire_hardware_fallback_balloon():
    _fire_hardware_fallback_balloon(self._icon)
```

(Implementer: place this near the existing balloon-firing call in the `_setup_icon` / `run` method or wherever the install-pending balloon fires today.)

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_launcher_tray.py -k hardware_fallback -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add helix_context/launcher/tray.py tests/test_launcher_tray.py
git commit -m "feat(tray): hardware-fallback balloon with state-change sentinel dedup

Mirrors the native-observability install-pending balloon pattern.
Sentinel at <state_dir>/.hardware-fallback-acknowledged-{req}-{act}
fires once per state-change combination."
```

---

### Task 14: Bench gate (manual)

**Files:**
- No code changes; produces bench JSONs + report

This is the spec's mandatory gate before merging PR1. Both runs use the same native sidecar instance to keep observability constant.

- [ ] **Step 1: Verify native sidecar is up**

Run:
```bash
netstat -ano 2>&1 | grep -E "[: ](4317|9090|3200|3100|3000) " | grep LISTENING | head -5
```
Expected: 5 LISTENING entries. If any port is missing, restart the tray launcher first.

- [ ] **Step 2: Capture master HEAD baseline**

```bash
git checkout master
git stash --include-untracked  # save any local working-tree state
bash overnight_diamond_native_n20_2026-05-04.sh   # script from sidecar PR
mv benchmarks/results/gpqa_native_n20_2026-05-04.json \
   benchmarks/results/gpqa_native_n20_master_baseline_pr1_$(date +%Y%m%d).json
```

(Use a fresh date suffix in the artifact name to avoid clobbering.)

- [ ] **Step 3: Capture PR1 HEAD result**

```bash
git checkout feat/hardware-detection
bash overnight_diamond_native_n20_2026-05-04.sh
mv benchmarks/results/gpqa_native_n20_2026-05-04.json \
   benchmarks/results/gpqa_native_n20_pr1_$(date +%Y%m%d).json
```

- [ ] **Step 4: Compute p95 delta + write the gate verdict**

Create a one-shot helper script `bench_gate_pr1.py` (local-only, do NOT commit):

```python
"""PR1 bench gate p95 delta. Run after Tasks 2-3 produce the JSON pair.
Exits 0 on PASS (delta <= 5s), 1 on FAIL.
"""
import json
import sys
from pathlib import Path

results_dir = Path("benchmarks/results")
master_files = sorted(results_dir.glob("gpqa_native_n20_master_baseline_pr1_*.json"))
pr1_files    = sorted(results_dir.glob("gpqa_native_n20_pr1_*.json"))
assert master_files and pr1_files, "Run Tasks 2-3 first to produce the JSONs."
master = json.loads(master_files[-1].read_text())
pr1    = json.loads(pr1_files[-1].read_text())

def p95(d):
    lats = sorted(r.get("proxy_latency_s", 0) for r in d["results"]
                  if not r.get("error") and r.get("proxy_latency_s", 0) > 0)
    return lats[int(0.95 * len(lats))] if lats else 0.0

m, p = p95(master), p95(pr1)
delta = p - m
print(f"p95(master) = {m:.2f}s")
print(f"p95(PR1)    = {p:.2f}s")
print(f"p95 delta   = {delta:+.2f}s   (gate: <= 5s)")
verdict = "PASS" if delta <= 5.0 else "FAIL"
print(f"VERDICT: {verdict}")
sys.exit(0 if verdict == "PASS" else 1)
```

Run: `python bench_gate_pr1.py`
Expected: prints PASS verdict.

- [ ] **Step 5: Decision**

If PASS: proceed to push the branch + open the PR.
If FAIL: investigate the regression. Most likely culprit per spec §10: chunked batches in deberta firing more torch kernel launches than the all-at-once tokenize. Fix is `min(recommended, len(input))` — only chunk if needed. Re-run after fix.

- [ ] **Step 6: Save the bench report (optional, dev-local)**

The artifacts in `benchmarks/results/gpqa_native_n20_*` stay on the dev rig (gitignored). PR body references the gate verdict + raw numbers; reviewers can ask for the JSONs if needed.

---

## After all 14 tasks

1. **Push the branch:** `git push -u origin feat/hardware-detection`
2. **Open the PR** with body referencing:
   - Spec link
   - Bench gate verdict (PASS, with p95 numbers from Task 14)
   - "PR2 (MPS + ROCm + CI workflow) follows" footer
3. **Tag for review**

The plan stops at PR1. PR2's plan is a separate cycle once PR1 lands and we've confirmed the bench gate holds in the master-merged form.
