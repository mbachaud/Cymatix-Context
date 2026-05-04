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
