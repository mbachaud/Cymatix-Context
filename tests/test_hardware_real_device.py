"""Opt-in real-hardware device tests (CUDA + ROCm).

Skipped by default. Set ``HELIX_TEST_CUDA=1`` on a host with CUDA-built
torch + NVIDIA GPU, or ``HELIX_TEST_ROCM=1`` on a Linux host with
ROCm-built torch (``torch.version.hip is not None``) + AMD GPU
passthrough, to exercise the full picker against real hardware. Mocked
device tests in ``tests/test_hardware.py`` cover the wiring; this
validates ``torch.cuda.mem_get_info``, ``get_device_name``, and the probe
round-trip on actual silicon. See spec
``docs/specs/2026-05-04-hardware-detection-design.md`` §8.3.

Formerly two byte-for-byte parallel files (``test_hardware_cuda_real.py``,
``test_hardware_rocm.py``) differing only in device string and env gate;
merged here with the device parametrized.
"""

from __future__ import annotations

import os

import pytest

from helix_context import hardware

requires_real_cuda = pytest.mark.skipif(
    os.environ.get("HELIX_TEST_CUDA") != "1",
    reason="Set HELIX_TEST_CUDA=1 on a CUDA-capable host to enable",
)

requires_rocm = pytest.mark.skipif(
    os.environ.get("HELIX_TEST_ROCM") != "1",
    reason="Set HELIX_TEST_ROCM=1 on a ROCm-capable host to enable",
)

_DEVICE_PARAMS = (
    pytest.param(
        "cuda", marks=[requires_real_cuda, pytest.mark.requires_real_cuda], id="cuda"
    ),
    pytest.param("rocm", marks=[requires_rocm, pytest.mark.requires_rocm], id="rocm"),
)


@pytest.fixture(autouse=True)
def _reset_hardware_cache():
    hardware.reset_for_test()
    yield
    hardware.reset_for_test()


@pytest.mark.parametrize("device", _DEVICE_PARAMS)
def test_auto_picker_lands_on_device(device):
    """On a <device>-capable host with the matching env gate set, auto-mode
    resolves to a <device>:N device with positive vram_total_gb and no
    fallback. On cuda, also asserts a non-empty device_name (parity with
    the original ``test_cuda_auto_picker_lands_on_cuda``)."""
    info = hardware.get_hardware()
    assert info.device_type == device, f"Expected {device}, got {info.device_type!r}"
    assert info.device.startswith(f"{device}:"), (
        f"Expected {device}:N, got {info.device!r}"
    )
    assert info.vram_total_gb is not None and info.vram_total_gb > 0, (
        f"vram_total_gb={info.vram_total_gb!r}; expected positive float on real hardware"
    )
    assert info.fallback_reason is None, (
        f"Unexpected fallback: {info.fallback_reason!r}"
    )
    if device == "cuda":
        assert info.device_name and isinstance(info.device_name, str), (
            f"device_name={info.device_name!r}; expected non-empty string"
        )


@pytest.mark.parametrize("device", _DEVICE_PARAMS)
def test_recommended_batch_size_is_positive(device):
    """recommended_batch_size returns a positive int for known models on device."""
    info = hardware.get_hardware()
    assert info.device_type == device
    for model in ("rerank", "splice", "splade", "nli"):
        bs = hardware.recommended_batch_size(model)
        assert isinstance(bs, int) and bs > 0, (
            f"recommended_batch_size({model!r})={bs!r}; expected positive int"
        )
