"""Unit tests for helix_context.hardware (mocked-torch).

All tests use monkeypatch to mock torch internals — they never touch a real
GPU. Pattern mirrors tests/test_observability_paths.py.
"""

from __future__ import annotations

import dataclasses
import pytest

from helix_context import hardware


@pytest.fixture(autouse=True)
def _reset(reset_hardware_cache):
    """Every test starts with a clean singleton (see tests/conftest.py)."""
    yield


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


@pytest.mark.parametrize(
    ("cpuinfo_result", "platform_processor", "expected_brand"),
    [
        pytest.param(
            {"brand_raw": "AMD Ryzen 9 7900X 12-Core Processor"},
            "should not be used",
            "AMD Ryzen 9 7900X 12-Core Processor",
            id="py-cpuinfo-brand-raw",
        ),
        pytest.param(
            None,
            "Intel64 Family 6 Model 158",
            "Intel64 Family 6 Model 158",
            id="platform-processor-fallback",
        ),
        pytest.param(
            None,
            "",
            "unknown CPU",
            id="terminal-fallback-unknown-cpu",
        ),
    ],
)
def test_cpu_brand_source_precedence(monkeypatch, cpuinfo_result, platform_processor, expected_brand):
    """cpu_brand source precedence: py-cpuinfo brand_raw > platform.processor() > 'unknown CPU'."""
    monkeypatch.setattr(
        "helix_context.hardware._cpuinfo_get_info",
        lambda: cpuinfo_result,
    )
    monkeypatch.setattr("platform.processor", lambda: platform_processor)
    info = hardware._detect_cpu()
    assert info["cpu_brand"] == expected_brand


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


@pytest.mark.parametrize(
    (
        "overrides",
        "expected_type",
        "expected_device",
        "expected_name_substring",
        "expected_vram_total_gb",
    ),
    [
        pytest.param(
            {
                "cuda_available": True,
                "cuda_device_count": 1,
                "cuda_device_names": ["NVIDIA GeForce RTX 4090"],
                "cuda_mem": [(22.4, 24.0)],
            },
            "cuda",
            "cuda:0",
            "NVIDIA GeForce RTX 4090",
            pytest.approx(24.0, rel=1e-3),
            id="cuda-available",
        ),
        pytest.param(
            {},
            "cpu",
            "cpu",
            None,
            None,
            id="nothing-available",
        ),
        pytest.param(
            # ROCm builds set torch.version.hip; cuda.is_available() also
            # returns True on a ROCm build (HIP devices surface through the
            # cuda API).
            {
                "cuda_available": True,
                "cuda_device_count": 1,
                "cuda_device_names": ["AMD Radeon RX 7900 XTX"],
                "cuda_mem": [(20.0, 24.0)],
                "hip_version": "5.7.0",
            },
            "rocm",
            "rocm:0",
            "Radeon",
            None,
            id="rocm-hip-advertised",
        ),
        pytest.param(
            {"mps_available": True, "mps_built": True},
            "mps",
            "mps",
            None,
            None,
            id="mps-only-available",
        ),
    ],
)
def test_auto_picks_device(
    mock_torch,
    overrides,
    expected_type,
    expected_device,
    expected_name_substring,
    expected_vram_total_gb,
):
    """Auto-mode device selection across cuda/cpu/rocm/mps backend flags."""
    for key, value in overrides.items():
        if key == "hip_version":
            mock_torch["set_hip"](value)
        else:
            mock_torch[key] = value
    info = hardware._detect()
    assert info.device_type == expected_type
    assert info.device == expected_device
    if expected_name_substring is not None:
        assert expected_name_substring in info.device_name
    if expected_vram_total_gb is not None:
        assert info.vram_total_gb == expected_vram_total_gb


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


def test_fallback_emits_summary_warning_for_headless_operators(
    mock_torch, monkeypatch, caplog
):
    """SF2 (#65): operators tailing logs on a headless deployment miss
    the tray balloon. _detect() must emit a single WARNING line tying the
    requested/active devices to the fallback_reason so the cause is in
    line-of-sight, not just a downstream /health field."""
    import logging
    monkeypatch.setenv("HELIX_DEVICE", "cuda")
    mock_torch["cuda_available"] = False
    with caplog.at_level(logging.WARNING, logger="helix.hardware"):
        info = hardware._detect()
    assert info.device_type == "cpu"
    assert info.fallback_reason is not None
    summary_lines = [
        rec.message
        for rec in caplog.records
        if "hardware fallback" in rec.message.lower()
    ]
    assert summary_lines, (
        "expected a 'Hardware fallback: ...' summary WARNING; got: "
        f"{[r.message for r in caplog.records]}"
    )
    msg = summary_lines[0].lower()
    assert "requested=cuda" in msg
    assert "active=cpu" in msg


def test_auto_picker_does_not_emit_fallback_warning(mock_torch, caplog):
    """When the user requested 'auto' and we landed on CPU, that's a
    normal outcome (no GPU on this box), not a fallback. The summary
    WARNING from SF2 must not fire — auto-on-CPU is not noteworthy."""
    import logging
    # mock_torch fixture defaults all backends to unavailable -> auto -> cpu
    with caplog.at_level(logging.WARNING, logger="helix.hardware"):
        info = hardware._detect()
    assert info.device_type == "cpu"
    assert info.fallback_reason is None
    summary_lines = [
        rec.message
        for rec in caplog.records
        if "hardware fallback" in rec.message.lower()
    ]
    assert not summary_lines, (
        f"auto-on-cpu should not fire the fallback WARNING; got: {summary_lines}"
    )


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
    import logging
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


@pytest.mark.parametrize(
    ("device_names", "cuda_mem", "expected_batch_sizes"),
    [
        pytest.param(
            ["RTX 4090"],
            (22.0, 24.0),
            {"rerank": 64, "splice": 128, "splade": 32, "nli": 32},
            id="24gb-cuda-tier",
        ),
        pytest.param(
            ["GTX 1650"],
            (3.5, 4.0),
            {"rerank": 8},
            id="4gb-cuda-tier",
        ),
        pytest.param(
            ["MX150"],
            (1.5, 2.0),
            {"rerank": 4},
            id="under-4gb-cuda-tier",
        ),
    ],
)
def test_batch_size_cuda_tiers(mock_torch, device_names, cuda_mem, expected_batch_sizes):
    """recommended_batch_size() picks the VRAM-tier table for cuda GPUs of various sizes."""
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = device_names
    mock_torch["cuda_mem"] = [cuda_mem]
    hardware.reset_for_test()
    for model, expected in expected_batch_sizes.items():
        assert hardware.recommended_batch_size(model) == expected


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


# ── Config flow (Task 9) ────────────────────────────────────────────
# init_from_config wires [hardware] section through to the singleton.

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
    assert hardware.recommended_batch_size("rerank") == 8


def test_init_from_config_must_run_before_get_hardware(mock_torch, monkeypatch):
    """Regression pin: if get_hardware() runs before init_from_config(),
    the cached singleton ignores config. This test documents that
    server startup MUST call init_from_config() first."""
    monkeypatch.setenv("HELIX_DEVICE", "auto")
    mock_torch["cuda_available"] = True
    mock_torch["cuda_device_count"] = 1
    mock_torch["cuda_device_names"] = ["RTX 4090"]
    mock_torch["cuda_mem"] = [(22.0, 24.0)]
    hardware.reset_for_test()
    info_early = hardware.get_hardware()
    assert info_early.batch_size_overrides == {}
    info_late = hardware.init_from_config(
        config_device="cuda",
        batch_size_overrides={"rerank": 8},
    )
    assert info_late.batch_size_overrides == {}  # cache wins; config lost
